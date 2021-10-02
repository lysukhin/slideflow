import os
from slideflow.util import log
if os.environ['SF_BACKEND'] == 'tensorflow':
    from slideflow.model.tensorflow import HyperParameters
    from slideflow.model.tensorflow import Trainer
    from slideflow.model.tensorflow import LinearTrainer
    from slideflow.model.tensorflow import CPHTrainer
elif os.environ['SF_BACKEND'] == 'torch':
    from slideflow.model.torch import HyperParameters
    from slideflow.model.torch import Trainer
else:
    raise ValueError(f"Unknown backend {os.environ['SF_BACKEND']}")
from slideflow.model.utils import get_hp_from_batch_file

class HyperParameterError(Exception):
    pass

class ManifestError(Exception):
    pass

class ModelError(Exception):
    def __init__(self, message, errors=None):
        log.error(message)
        super().__init__(message)

def trainer_from_hp(hp, **kwargs):
    """From the given :class:`slideflow.model.HyperParameters` object, returns the appropriate instance of
    :class:`slideflow.model.Model`.

    Args:
        hp (:class:`slideflow.model.HyperParameters`): HyperParameters object.

    Keyword Args:
        outdir (str): Location where event logs and checkpoints will be written.
        annotations (dict): Nested dict, mapping slide names to a dict with patient name (key 'submitter_id'),
            outcome labels (key 'outcome_label'), and any additional slide-level inputs (key 'input').
        name (str, optional): Optional name describing the model, used for model saving. Defaults to None.
        manifest (dict, optional): Manifest dictionary mapping TFRecords to number of tiles. Defaults to None.
        model_type (str, optional): Type of model outcome, 'categorical' or 'linear'. Defaults to 'categorical'.
        feature_sizes (list, optional): List of sizes of input features. Required if providing additional
            input features as input to the model.
        feature_names (list, optional): List of names for input features. Used when permuting feature importance.
        normalizer (str, optional): Normalization strategy to use on image tiles. Defaults to None.
        normalizer_source (str, optional): Path to normalizer source image. Defaults to None.
            If None but using a normalizer, will use an internal tile for normalization.
            Internal default tile can be found at slideflow.util.norm_tile.jpg
        outcome_names (list, optional): Name of each outcome. Defaults to "Outcome {X}" for each outcome.
        mixed_precision (bool, optional): Use FP16 mixed precision (rather than FP32). Defaults to True.
    """

    if hp.model_type() == 'categorical':
        return Trainer(hp, **kwargs)
    if hp.model_type() == 'linear':
        return LinearTrainer(hp, **kwargs)
    if hp.model_type() == 'cph':
        return CPHTrainer(hp, **kwargs)
    else:
        raise ModelError(f"Unknown model type: {hp.model_type()}")