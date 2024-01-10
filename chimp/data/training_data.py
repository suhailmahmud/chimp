"""
chimp.data.training_data
=======================

Interface classes for loading the CHIMP training data.
"""
from dataclasses import dataclass
from datetime import datetime
from math import ceil
import os
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Union

import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
from matplotlib.gridspec import GridSpec
from matplotlib.animation import FuncAnimation
import numpy as np
from scipy import fft
from scipy import ndimage
import torch
from torch import nn
from torch.utils.data import IterableDataset
import torch.distributed as dist
import torchvision
from torchvision.transforms.functional import center_crop
import xarray as xr
import pandas as pd

from pytorch_retrieve.tensors.masked_tensor import MaskedTensor


from chimp import data
from chimp.definitions import MASK
from chimp.utils import get_date
from chimp.data import get_reference_data
from chimp.data import get_input, get_reference_data
from chimp.data import input, reference


def generate_input(
    inpt: data.Input,
    size: Tuple[int],
    policy: str,
    rng: np.random.Generator,
):
    """
    Generate input values for missing inputs.

    Args:
        inpt: The input object for which to generate the input.
        size: Side-length of the quadratic input array.
        value: The value to fill the array with.
        policy: The policy to use for the data generation.
        rng: The random generator object to use to create random
            arrays.

    Return:
        An numpy.ndarray containing replacement data.
    """
    if policy == "sparse":
        return None
    elif policy == "random":
        return rng.normal(size=(inpt.n_channels,) + size)
    elif policy == "mean":
        return inpt.normalizer(
            inpt.mean * np.ones(shape=(inpt.n_channels, size, size), dtype="float32")
        )
    elif policy == "missing":
        return inpt.normalizer(
            np.nan * np.ones(shpare=(inpt.n_channels, size, size), dtype="float32")
        )

    raise ValueError(
        f"Missing input policy '{policy}' is not known. Choose between 'random'"
        " 'mean' and 'constant'. "
    )


@dataclass
class SampleRecord:
    """
    Record holding the paths of the files for a single training
    sample.
    """

    radar: Path = None
    geo: Path = None
    mw: Path = None
    visir: Path = None

    def has_input(self, sources):
        """
        Determine if sample has input from any of the given sources.

        Args:
            sources: A list of sources to require.

        Return:
            Bool if the sample has corresponding input data from any of
            the given sources.
        """
        has_input = False
        for source in sources:
            if getattr(self, source) is not None:
                has_input = True
        return has_input


class SingleStepDataset:
    """
    Dataset class for the CHIMP training data.

    Implements the PyTorch Dataset interface.
    """

    def __init__(
        self,
        path: Path,
        input_datasets: List[str],
        reference_datasets: List[str],
        sample_rate: int = 1,
        scene_size: int = 128,
        start_time: Optional[np.datetime64] = None,
        end_time: Optional[np.datetime64] = None,
        augment: bool = True,
        missing_value_policy: str = "sparse",
        time_step: Optional[np.timedelta64] = None,
        validation: bool = False,
        quality_threshold: Union[float, List[float]] = 0.8
    ):
        """
        Args:
            path: The root directory containing the training data.
            input_datasets: List of the input datasets or their names from which
                to load the retrieval input data.
            reference_datasets: List of the reference datasets or their names
                from which to load the reference data.
            sample_rate: How often each scene should be sampled per epoch.
            scene_size: Size of the training scenes.
            start_time: Start time of time interval to which to restrict
                training data.
            end_time: End time of a time interval to which to restrict the
                training data.
            augment: Whether to apply random transformations to the training
                inputs.
            missing_value_policy: A string indicating how to handle missing input
                data. Options:
                    'random': Missing data is replaced with Gaussian noise.
                    'mean' Missing value is replaced with the mean of the
                    input data.
                    'missing': Missing data is replaced with NANs
                    'sparse': Instead of a tensor 'None' is returned.
            time_step: Minimum time step between consecutive reference samples.
                Can be used to sub-sample the reference data.
            validation: If 'True', repeated sampling will reproduce identical scenes.
            quality_threshold: Thresholds for the quality indices applied to limit
                reference data pixels.
        """
        self.path = Path(path)

        self.input_datasets = [
            get_input(input_dataset) for input_dataset in input_datasets
        ]
        self.reference_datasets = [
            get_reference_data(reference_dataset)
            for reference_dataset in reference_datasets
        ]

        self.sample_rate = sample_rate
        self.augment = augment
        self.missing_value_policy = missing_value_policy

        n_input_datasets = len(self.input_datasets)
        n_reference_datasets = len(self.reference_datasets)
        n_datasets = n_input_datasets + n_reference_datasets

        sample_files = {}
        for ref_ind, reference_dataset in enumerate(self.reference_datasets):
            files = reference_dataset.find_files(self.path)
            times = np.array(list(map(get_date, files)))
            for time, filename in zip(times, files):
                files = sample_files.setdefault(time, ([None] * n_datasets))
                files[ref_ind] = filename

        if len(sample_files) == 0:
            raise RuntimeError(
                f"Found no reference data files in path '{self.path}' for "
                f" reference data '{self.reference_data.name}'."
            )

        for input_ind, input_dataset in enumerate(self.input_datasets):
            input_files = input_dataset.find_files(self.path)
            times = np.array(list(map(get_date, input_files)))
            for time, input_file in zip(times, input_files):
                if time in sample_files:
                    sample_files[time][n_reference_datasets + input_ind] = input_file

        self.base_scale = min(
            [reference_dataset.scale for reference_dataset in self.reference_datasets]
        )
        self.max_scale = 0
        self.scales = {}
        for input_dataset in self.input_datasets:
            scale = input_dataset.scale // self.base_scale
            self.scales[input_dataset.name] = scale
            self.max_scale = max(self.max_scale, scale)

        times = np.array(list(sample_files.keys()))
        samples = np.array(list(sample_files.values()))
        reference_files = samples[:, :n_reference_datasets]
        input_files = samples[:, n_reference_datasets:]

        if start_time is not None and end_time is not None:
            indices = (times >= start_time) * (times < end_time)
            times = times[indices]
            reference_files = reference_files[indices]
            input_files = input_files[indices]

        if time_step is not None:
            d_t_files = times[1] - times[0]
            subsample = int(
                time_step.astype("timedelta64[s]") /
                d_t_files.astype("timedelta64[s]")
            )
            times = times[::subsample]
            reference_files = reference_files[::subsample]
            input_files = input_files[::subsample]

        self.times = times
        self.reference_files = reference_files
        self.input_files = input_files

        # Ensure that data is consistent
        assert len(self.times) == len(self.reference_files)
        assert len(self.times) == len(self.input_files)

        self.scene_size = scene_size
        self.validation = validation
        if self.validation:
            self.augment = False
        self.init_rng()

        if isinstance(quality_threshold, float):
            quality_threshold = [quality_threshold] * n_reference_datasets
        self.quality_threshold = quality_threshold


    def init_rng(self, w_id=0):
        """
        Initialize random number generator.

        Args:
            w_id: The worker ID which of the worker process..
        """
        if self.validation:
            seed = 1234
        else:
            seed = int.from_bytes(os.urandom(4), "big") + w_id
        self.rng = np.random.default_rng(seed)

    def worker_init_fn(self, *args):
        """
        Pytorch retrieve interface.
        """
        return self.init_rng(*args)

    def load_reference_sample(
        self,
        files: Tuple[Path],
        slices: Optional[Tuple[int]],
        scene_size: int,
        forecast: bool = False,
        rotate: Optional[float] = None,
        flip: bool = False,
    ):
        """
        Load training sample.

        Args:
            files: A list containing the reference-data and input files
                 containing the data from which to load this sample.
            slices: Tuple ``(i_start, i_end, j_start, j_end)`` defining
                defining the crop of the domain. If set to 'None', the full
                domain is loaded.
            scene_size: The window size specified with respect to the
                reference data.
            forecast: If 'True', no input data will be loaded and all inputs
                will be set to None.
            rotate: If provided, should be float specifying the degree by which
                to rotate the input.
            flip: If 'True', input will be flipped along the last axis.

        Return:
            A tuple ``(x, y)`` containing two dictionaries ``x`` and ``y``
            with ``x`` containing the training inputs and ``y`` the
            corresponding outputs.
        """
        if isinstance(scene_size, int):
            scene_size = (scene_size,) * 2

        if slices is not None:
            i_start, i_end, j_start, j_end = slices
            row_slice = slice(i_start, i_end)
            col_slice = slice(j_start, j_end)
        else:
            row_slice = slice(0, None)
            col_slice = slice(0, None)

        # Load reference data.
        y = {}
        for dataset_ind, reference_dataset in enumerate(self.reference_datasets):
            y.update(
                reference_dataset.load_sample(
                    files[dataset_ind],
                    scene_size,
                    self.base_scale,
                    slices,
                    self.rng,
                    rotate=rotate,
                    flip=flip,
                    quality_threshold=self.quality_threshold[dataset_ind]
                )
            )
        return y


    def load_input_sample(
        self,
        files: Tuple[Path],
        slices: Optional[Tuple[int]],
        scene_size: int,
        forecast: bool = False,
        rotate: Optional[float] = None,
        flip: bool = False,
    ) -> Dict[str, torch.Tensor]:
        """
        Load input for given training sample.

        Args:
            files: Numpy array containing the paths to the files to load.
            slices: Tuple ``(i_start, i_end, j_start, j_end)`` defining
                defining the crop of the domain. If set to 'None', the full
                domain is loaded.
            scene_size: The window size specified with respect to the
                reference data.
            forecast: If 'True', no input data will be loaded and all inputs
                will be set to None.
            rotate: If provided, should be float specifying the degree by which
                to rotate the input.
            flip: If 'True', input will be flipped along the last axis.

        Return:
            A dictionary mapping input dataset names to corresponding input
            data tensors.
        """
        if isinstance(scene_size, int):
            scene_size = (scene_size,) * 2

        if slices is not None:
            i_start, i_end, j_start, j_end = slices
            row_slice = slice(i_start, i_end)
            col_slice = slice(j_start, j_end)
        else:
            row_slice = slice(0, None)
            col_slice = slice(0, None)

        x = {}
        for input_ind, input_dataset in enumerate(self.input_datasets):
            input_file = files[input_ind]
            x_s = input_dataset.load_sample(
                input_file,
                scene_size,
                self.base_scale,
                slices,
                self.rng,
                self.missing_value_policy,
                rotate=rotate,
                flip=flip,
            )
            x[input_dataset.name] = x_s
        return x

    def __len__(self):
        """Number of samples in dataset."""
        return len(self.times) * self.sample_rate

    def __getitem__(self, index):
        """Return ith training sample."""
        n_samples = len(self.times)
        sample_index = index % n_samples

        # We load a larger window when input is rotated to avoid
        # missing values.
        if self.augment:
            scene_size = int(1.42 * self.scene_size)
            rem = scene_size % self.max_scale
            if rem != 0:
                scene_size += self.max_scale - rem
            ang = -180 + 360 * self.rng.random()
            flip = self.rng.random() > 0.5
        else:
            scene_size = self.scene_size
            ang = None
            flip = False

        slices = reference.find_random_scene(
            self.reference_datasets[0],
            self.reference_files[sample_index][0],
            self.rng,
            multiple=4,
            scene_size=scene_size,
            quality_threshold=self.quality_threshold[0]
        )

        x = self.load_input_sample(
            self.input_files[sample_index], slices, self.scene_size, rotate=ang, flip=flip
        )
        y = self.load_reference_sample(
            self.reference_files[sample_index], slices, self.scene_size, rotate=ang, flip=flip
        )

        return x, y



class CHIMPPretrainDataset(SingleStepDataset):
    """
    Dataset class for the CHIMP training data.

    Implements the PyTorch Dataset interface.
    """

    def __init__(
        self,
        path: Path,
        input_datasets: List[str],
        reference_datasets: List[str],
        sample_rate=1,
        scene_size=128,
        start_time=None,
        end_time=None,
        augment=True,
        missing_value_policy="sparse",
        time_step=None,
        quality_threshold=0.8,
    ):
        super().__init__(
            path,
            input_datasets,
            reference_datasets,
            scene_size=scene_size,
            start_time=start_time,
            end_time=end_time,
            augment=augment,
            missing_value_policy=missing_value_policy,
            time_step=time_step,
            quality_threshold=quality_threshold,
        )
        samples_by_input = [[] for _ in self.input_datasets]
        for scene_index in range(len(self.times)):
            input_files = self.input_files[scene_index]
            for input_ind in range(len(self.input_datasets)):
                # Input not available at time step
                if input_files[input_ind] is None:
                    continue

                with xr.open_dataset(input_files[input_ind]) as data:
                    if "swath_centers" not in data.dims:
                        samples_by_input[input_ind].append(scene_index)
                    else:
                        if data.swath_centers.size > 0:
                            samples_by_input[input_ind].append(scene_index)

        most_obs = max(map(len, samples_by_input))
        total_samples = len(self.input_datasets) * most_obs
        sample_indices = []
        for input_ind in range(len(self.input_datasets)):
            sample_indices.append(
                self.rng.choice(samples_by_input[input_ind], most_obs, replace=True)
            )
        self.sample_indices = np.concatenate(sample_indices)
        self.obs_per_input = most_obs

    def __len__(self):
        return len(self.sample_indices)  * self.sample_rate

    def __getitem__(self, index):
        """Return ith training sample."""
        scene_index = self.sequence_starts[index // self.sample_rate]
        input_index = index // self.obs_per_input

        scl = inpt.scale // self.base_scale
        slices = self.input_datasets[input_index].find_random_scene(
            self.samples[scene_index][1 + input_index],
            self.rng,
            multiple=16 // inpt.scale,
            scene_size=self.scene_size // scl,
            rqi_thresh=self.quality_threshold,
        )

        if slices is None:
            new_index = self.rng.integers(0, len(self))
            return self[new_index]

        slices = tuple((index * scl for index in slices))

        xs = []
        ys = []

        if self.augment:
            ang = -180 + 360 * self.rng.random()
            flip = self.rng.random() > 0.5
        else:
            ang = None
            flip = False

        x = self.load_input_sample(
            self.samples[scene_index], slices, self.scene_size, rotate=ang, flip=flip
        )
        y = self.load_reference_sample(
            self.samples[scene_index], slices, self.scene_size, rotate=ang, flip=flip
        )

        return x, y


class SequenceDataset(SingleStepDataset):
    """
    Dataset class for temporal merging of satellite observations.
    """

    def __init__(
        self,
        path: Path,
        input_datasets: List[str],
        reference_datasets: List[str],
        sample_rate: int = 2,
        scene_size: int = 256,
        sequence_length: int = 32,
        forecast: int = 0,
        include_input_steps: bool = True,
        start_time: np.datetime64 = None,
        end_time: np.datetime64 = None,
        quality_threshold: float = 0.8,
        missing_value_policy: str = "masked",
        augment: bool = True,
        shrink_output: Optional[int] = None,
        validation: bool = False,
        time_step: Optional[np.timedelta64] = None,
    ):
        """
        Args:
            path: The path to the training data.
            input_datasets: List of input datasets or their names from which to load
                 the input data.
            reference_datasets: List of reference datasets or their names from which
                 to load the reference data.
            sample_rate: Rate for oversampling of training scenes.
            scene_size: The size of the input data.
            sequence_length: The length of input data sequences.
            forecast: The number of time steps to forecast.
            include_input_steps: Whether reference data for the input steps should
                be loaded as well.
            start_time: Optional start time to limit the samples.
            end_time: Optional end time to limit the available samples.

            augment: Whether to apply random transformations to the training
                inputs.
            forecast: The number of samples in the sequence without input
                observations.
            shrink_output: If given, the reference data scenes will contain
                only the center crop the total scene with the size of the
                crop calculated by dividing the input size by the given factor.
            validation: If 'True' sampling will reproduce identical scenes.
            quality_threshold: Thresholds for the quality indices applied to limit
                reference data pixels.
        """
        super().__init__(
            path,
            input_datasets,
            reference_datasets,
            sample_rate=sample_rate,
            scene_size=scene_size,
            start_time=start_time,
            end_time=end_time,
            missing_value_policy=missing_value_policy,
            quality_threshold=quality_threshold,
            augment=augment,
            validation=validation,
        )

        self.sequence_length = sequence_length
        self.forecast = forecast
        total_length =  sequence_length + forecast
        self.total_length = total_length
        self.include_input_steps = include_input_steps
        self.shrink_output = shrink_output

        # Find samples with a series of consecutive outputs.
        times = self.times
        deltas = times[self.total_length:] - times[:-self.total_length]
        if time_step is None:
            time_step = deltas.min()
        self.time_step = time_step
        self.sequence_starts = np.where(
            deltas.astype("timedelta64[s]") <= sequence_length * time_step
        )[0]

    def __len__(self):
        """Number of samples in an epoch."""
        return len(self.sequence_starts) * self.sample_rate

    def __getitem__(self, index):
        """Return training sample."""
        index = index // self.sample_rate

        # We load a larger window when input is rotated to avoid
        # missing values.
        if self.augment:
            scene_size = int(1.42 * self.scene_size)
            rem = scene_size % self.max_scale
            if rem != 0:
                scene_size += self.max_scale - rem
            ang = -180 + 360 * self.rng.random()
            flip = self.rng.random() > 0.5
        else:
            scene_size = self.scene_size
            ang = None
            flip = False

        # Find valid input range for last sample in sequence
        last_index = self.sequence_starts[index] + self.total_length
        slices = reference.find_random_scene(
            self.reference_datasets[0],
            self.reference_files[last_index][0],
            self.rng,
            multiple=4,
            scene_size=scene_size,
            quality_threshold=self.quality_threshold[0],
        )

        x = {}
        y = {}

        start_index = self.sequence_starts[index]
        for step in range(self.total_length):
            step_index = start_index + step
            if step < self.sequence_length:
                x_i = self.load_input_sample(
                    self.input_files[step_index], slices, self.scene_size, rotate=ang, flip=flip
                )
                for name, inpt in x_i.items():
                    x.setdefault(name, []).append(inpt)
                if self.include_input_steps:
                    y_i = self.load_reference_sample(
                        self.reference_files[step_index], slices, self.scene_size, rotate=ang, flip=flip
                    )
                    if self.shrink_output:
                        y_i = {
                            name: center_crop(
                                tensor, tensor.shape[-1] // self.shrink_output
                            ) for name, tensor in y_i.items()
                        }
                    for name, inpt in y_i.items():
                        y.setdefault(name, []).append(inpt)
            else:
                y_i = self.load_reference_sample(
                    self.reference_files[step_index], slices, self.scene_size, rotate=ang, flip=flip
                )
                for name, inpt in y_i.items():
                    y.setdefault(name, []).append(inpt)

        if self.forecast > 0:
            x["lead_times"] = torch.tensor(
                [
                    step * self.time_step.astype("int64") // 60
                    for step in range(1, self.forecast + 1)
                ]
            )
        return x, y


def plot_sample(x, y):
    """
    Plot input and output from a sample.

    Args:
        x: The training input.
        y: The training output.
    """
    if not isinstance(x, list):
        x = [x]
    if not isinstance(y, list):
        y = [y]

    n_steps = len(x)

    for i in range(n_steps):
        f, axs = plt.subplots(1, 2, figsize=(10, 5))

        ax = axs[0]
        ax.imshow(x[i]["geo"][-1])

        ax = axs[1]
        ax.imshow(y[i])


def plot_date_distribution(path, keys=None, show_sensors=False, ax=None):
    """
    Plot the number of training input samples per day.

    Args:
        path: Path to the directory that contains the training data.
        keys: A list file prefixes to look for, e.g. ['geo', 'visir'] to
            only list geostationary and AVHRR inputs.
        show_sensors: Whether to show different microwave sensors separately.
        ax: A matplotlib Axes object to use to draw the results.
    """
    if keys is None:
        keys = ["seviri", "mw", "radar", "visir"]
    if not isinstance(keys, list):
        keys = list(keys)

    times = {}
    time_min = None
    time_max = None
    for key in keys:
        files = list(Path(path).glob(f"**/{key}*.nc"))
        times_k = [
            datetime.strptime(name.name[len(key) + 1 : -3], "%Y%m%d_%H_%M")
            for name in files
        ]

        if key == "mw" and show_sensors:
            sensors = {}
            for time_k, filename in zip(times_k, files):
                with xr.open_dataset(filename) as data:
                    satellite = data.attrs["satellite"]
                    sensor = data.attrs["sensor"]
                    sensor = f"{sensor}"
                    sensor_times = sensors.setdefault(sensor, [])
                    sensor_times.append(time_k)

            for sensor in sensors:
                times_s = sensors[sensor]
                times_k = xr.DataArray(times_s)
                times[sensor] = times_k
                t_min = times_k.min()
                time_min = min(time_min, t_min) if time_min is not None else t_min
                t_max = times_k.max()
                time_max = max(time_max, t_max) if time_max is not None else t_max
        else:
            times_k = xr.DataArray(times_k)
            t_min = times_k.min()
            time_min = min(time_min, t_min) if time_min is not None else t_min
            t_max = times_k.max()
            time_max = max(time_max, t_max) if time_max is not None else t_max

            times[key] = times_k

    if ax is None:
        figure = plt.figure(figsize=(6, 4))
        ax = figure.add_subplot(1, 1, 1)

    bins = np.arange(
        time_min.astype("datetime64[D]").data,
        (time_max + np.timedelta64(2, "D")).astype("datetime64[D]").data,
        dtype="datetime64[D]",
    )
    x = bins[:-1] + 0.5 * (bins[1:] - bins[:-1])

    for key in times:
        y, _ = np.histogram(times[key], bins=bins)
        ax.plot(x, y, label=key)

    ax.legend()
    return ax