"""
==============
cimr.bin.train
==============

This sub-module implements the cimr CLI to train the retrieval.
"""
from calendar import monthrange
import logging
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import importlib
import multiprocessing as mp
from pathlib import Path
import sys

import numpy as np


LOGGER = logging.getLogger(__name__)


def add_parser(subparsers):
    """
    Add parser for 'train' command to top-level parser. This function
    is called from the top-level parser defined in 'cimr.bin'.

    Args:
        subparsers: The subparsers object provided by the top-level parser.
    """
    parser = subparsers.add_parser(
        "train",
        help="Train retrieval network.",
        description=(
            """
            Train the retrieval network.
            """
        ),
    )
    parser.add_argument(
        "model_name",
        metavar="name",
        type=str,
        help=(
            "A name identifying the model to be trained."
        )
    )
    parser.add_argument(
        "training_data",
        metavar="training_data",
        type=str,
        help="Folder containing the training data.",
    )

    parser.add_argument(
        "model_config",
        metavar="model_config",
        type=str,
        help=(
            "Path to a model configuration file, an existing model, "
            " or a model checkpoint."
        )
    )

    parser.add_argument(
        "training_config",
        metavar="training_config",
        type=str,
        help=(
            "Path to a training configuration file specifying the"
            " training regime."
        )
    )
    parser.add_argument(
        "--validation_data",
        metavar="path",
        type=str,
        help="Folder containing the validation data.",
        default=None
    )
    parser.add_argument(
        "--batch_size",
        metavar="N",
        type=int,
        default=4,
        help="The batch size to use during training.",
    )
    parser.add_argument(
        "--output_path",
        metavar="path",
        type=str,
        default=".",
        help=(
            "Path pointing to a directory at which to store the training "
            " results."
        )
    )
    parser.add_argument(
        "--resume",
        action="store_false"
    )

    parser.set_defaults(func=run)


def run(args):
    """
    Run training.

    Args:
        args: The namespace object provided by the top-level parser.
    """
    from torch.optim import AdamW, SGD
    from torch.optim.lr_scheduler import CosineAnnealingLR
    import pytorch_lightning as pl
    from pytorch_lightning.callbacks import LearningRateMonitor, Callback
    from pytorch_lightning.strategies import DDPStrategy
    from quantnn.qrnn import QRNN
    from quantnn import metrics
    from quantnn import transformations
    from cimr import models
    from cimr.config import parse_model_config, parse_training_config
    from cimr.training import train
    from cimr.models import compile_mrnn

    #
    # Prepare training and validation data.
    #


    training_data = Path(args.training_data)
    if not training_data.exists():
        LOGGER.error(
            "Provided training data path '%s' doesn't exist."
        )
        sys.exit()

    validation_data = args.validation_data
    if validation_data is not None:
        validation_data = Path(validation_data)
        if not validation_data.exists():
            LOGGER.error(
                "Provided validation data path '%s' doesn't exist."
            )
            sys.exit()

    model_config = Path(args.model_config)
    if not model_config.exists():
        LOGGER.error(
            "Model argument must point to an existing model configuration "
            " file, or an existing model or training checkpoint."
        )
        sys.exit()
    model_config = parse_model_config(model_config)
    mrnn = compile_mrnn(model_config)

    output_path = Path(args.output_path)
    model_path = output_path / args.model_name
    ckpt_path = model_path / f"cimr_{args.model_name}.ckpt"

    if ckpt_path.exists():
        if args.resume:
            LOGGER.info(
                f"Continuing training from checkpoint at '{ckpt_path}'."
            )
        else:
            LOGGER.info(
                f"Not continuing from checkpoint checkpoint '{ckpt_path}' "
                " because --resume flag has not been set."
            )


        ckpt_path = None

    training_config_path = Path(args.training_config)
    if not training_config_path.exists():
        LOGGER.error(
            "'training_config' must point to an existing training config "
            "file."
        )
        sys.exit()

    training_configs = parse_training_config(args.training_config)

    train(
        args.model_name,
        mrnn,
        training_configs,
        training_data,
        validation_data,
        output_path=output_path,
        ckpt_path=ckpt_path
    )