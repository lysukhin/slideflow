![slideflow logo](https://github.com/jamesdolezal/slideflow/raw/master/docs-source/pytorch_sphinx_theme/images/slideflow-banner.png)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.5703792.svg)](https://doi.org/10.5281/zenodo.5703792)
[![Python application](https://github.com/jamesdolezal/slideflow/actions/workflows/python-app.yml/badge.svg?branch=master)](https://github.com/jamesdolezal/slideflow/actions/workflows/python-app.yml)
[![PyPI version](https://badge.fury.io/py/slideflow.svg)](https://badge.fury.io/py/slideflow)

[ArXiv](https://arxiv.org/abs/2304.04142) | [Docs](https://slideflow.dev) | [Slideflow Studio](https://slideflow.dev/studio/) | [Cite](#reference)

Slideflow is a deep learning library for digital pathology that provides a unified API for building, training, and testing models using Tensorflow or PyTorch.

Slideflow includes tools for **[whole-slide image processing](https://slideflow.dev/slide_processing)**, **customizable deep learning [model training](https://slideflow.dev/training)** with dozens of supported architectures, **[multi-instance learning](https://slideflow.dev/mil)**, **[self-supervised learning](https://slideflow.dev/ssl)**, **[cell segmentation](https://slideflow.dev/cellseg)**, **explainability tools** (including [heatmaps](https://slideflow.dev/evaluation/#heatmaps), [mosaic maps](https://slideflow.dev/posthoc/#mosaic-maps), [GANs](https://slideflow.dev/stylegan/), and [saliency maps](https://slideflow.dev/saliency/)), **analysis of [layer activations](https://slideflow.dev/posthoc/)**, **[uncertainty quantification](https://slideflow.dev/uq/)**, and more.

A variety of fast, optimized whole-slide image processing tools are included, including background filtering, blur/artifact detection, [stain normalization/augmentation](https://slideflow.dev/norm), and efficient storage in `*.tfrecords` format. Model training is easy and highly configurable, with an straightforward API for training custom architectures. Slideflow can be used as an image processing backend for external training loops, serving an optimized `tf.data.Dataset` or `torch.utils.data.DataLoader` to read and process slide images and perform real-time stain normalization.

Full documentation with example tutorials can be found at [slideflow.dev](https://www.slideflow.dev/).

![Studio preview](https://slideflow.dev/_images/studio_saliency.jpg)
*Slideflow Studio: a visualization tool for interacting with models and whole-slide images.*

## Requirements
- Python >= 3.7 (<3.10 if using [cuCIM](https://docs.rapids.ai/api/cucim/stable/))
- [Tensorflow](https://www.tensorflow.org/) 2.5-2.11 _or_ [PyTorch](https://pytorch.org/) 1.9-2.0
  - GAN and MIL functions require PyTorch <1.13

### Optional
- [Libvips](https://libvips.github.io/libvips/) >= 8.9 (alternative slide reader, adds support for *.scn, *.mrxs, *.ndpi, *.vms, and *.vmu files).
- Linear solver (for preserved-site cross-validation)
  - [CPLEX](https://www.ibm.com/docs/en/icos/12.10.0?topic=v12100-installing-cplex-optimization-studio) 20.1.0 with [Python API](https://www.ibm.com/docs/en/icos/12.10.0?topic=cplex-setting-up-python-api)
  - _or_ [Pyomo](http://www.pyomo.org/installation) with [Bonmin](https://anaconda.org/conda-forge/coinbonmin) solver


## Installation
Slideflow can be installed with PyPI, as a Docker container, or run from source.

### Method 1: Install via pip

```
pip3 install --upgrade setuptools pip wheel
pip3 install slideflow[cucim] cupy-cuda11x
```

The `cupy` package name depends on the installed CUDA version; [see here](https://docs.cupy.dev/en/stable/install.html#installing-cupy) for installation instructions. `cupy` is not required if using Libvips.

### Method 2: Docker image

Alternatively, pre-configured [docker images](https://hub.docker.com/repository/docker/jamesdolezal/slideflow) are available with OpenSlide/Libvips and the latest version of either Tensorflow and PyTorch. To install with the Tensorflow backend:

```
docker pull jamesdolezal/slideflow:latest-tf
docker run -it --gpus all jamesdolezal/slideflow:latest-tf
```

To install with the PyTorch backend:

```
docker pull jamesdolezal/slideflow:latest-torch
docker run -it --shm-size=2g --gpus all jamesdolezal/slideflow:latest-torch
```

### Method 3: From source

To run from source, clone this repository, install the conda development environment, and build a wheel:

```
git clone https://github.com/jamesdolezal/slideflow
cd slideflow
conda env create -f environment.yml
conda activate slideflow
python setup.py bdist_wheel
pip install dist/slideflow* cupy-cuda11x
```

## Configuration

### Deep learning (Tensorflow vs. PyTorch)

Slideflow supports both Tensorflow and PyTorch, defaulting to Tensorflow if both are available. You can specify the backend to use with the environmental variable `SF_BACKEND`. For example:

```
export SF_BACKEND=torch
```

### Slide reading (cuCIM vs. Libvips)

By default, Slideflow reads whole-slide images using [cuCIM](https://docs.rapids.ai/api/cucim/stable/). Although much faster than other openslide-based frameworks, it supports fewer slide scanner formats. Slideflow also includes a [Libvips](https://libvips.github.io/libvips/) backend, which adds support for *.scn, *.mrxs, *.ndpi, *.vms, and *.vmu files. You can set the active slide backend with the environmental variable `SF_SLIDE_BACKEND`:

```
export SF_SLIDE_BACKEND=libvips
```


## Getting started
Slideflow experiments are organized into [Projects](https://slideflow.dev/project_setup), which supervise storage of whole-slide images, extracted tiles, and patient-level annotations. The fastest way to get started is to use one of our preconfigured projects, which will automatically download slides from the Genomic Data Commons:

```python
import slideflow as sf

P = sf.create_project(
    root='/project/destination',
    cfg=sf.project.LungAdenoSquam,
    download=True
)
```

After the slides have been downloaded and verified, you can skip to [Extract tiles from slides](#extract-tiles-from-slides).

Alternatively, to create a new custom project, supply the location of patient-level annotations (CSV), slides, and a destination for TFRecords to be saved:

```python
import slideflow as sf
P = sf.create_project(
  '/project/path',
  annotations="/patient/annotations.csv",
  slides="/slides/directory",
  tfrecords="/tfrecords/directory"
)
```

Ensure that the annotations file has a `slide` column for each annotation entry with the filename (without extension) of the corresponding slide.

## Extract tiles from slides

Next, whole-slide images are segmented into smaller image tiles and saved in `*.tfrecords` format. [Extract tiles](https://slideflow.dev/slide_processing) from slides at a given magnification (width in microns size) and resolution (width in pixels) using `sf.Project.extract_tiles()`:

```python
P.extract_tiles(
  tile_px=299,  # Tile size, in pixels
  tile_um=302   # Tile size, in microns
)
```

If slides are on a network drive or a spinning HDD, tile extraction can be accelerated by buffering slides to a SSD or ramdisk:

```python
P.extract_tiles(
  ...,
  buffer="/mnt/ramdisk"
)
```

## Training models

Once tiles are extracted, models can be [trained](https://slideflow.dev/training). Start by configuring a set of [hyperparameters](https://slideflow.dev/model#modelparams):

```python
params = sf.ModelParams(
  tile_px=299,
  tile_um=302,
  batch_size=32,
  model='xception',
  learning_rate=0.0001,
  ...
)
```

Models can then be trained using these parameters. Models can be trained to categorical, multi-categorical, continuous, or time-series outcomes, and the training process is [highly configurable](https://slideflow.dev/training). For example, to train models in cross-validation to predict the outcome `'category1'` as stored in the project annotations file:

```python
P.train(
  'category1',
  params=params,
  save_predictions=True,
  multi_gpu=True
)
```

## Evaluation, heatmaps, mosaic maps, and more

Slideflow includes a host of additional tools, including model [evaluation and prediction](https://slideflow.dev/evaluation), [heatmaps](https://slideflow.dev/evaluation#heatmaps), analysis of [layer activations](https://slideflow.dev/posthoc), [mosaic maps](https://slideflow.dev/posthoc#mosaic-maps), and more. See our [full documentation](https://slideflow.dev) for more details and tutorials.

## Publications

Slideflow has been used by:

- [Dolezal et al](https://www.nature.com/articles/s41379-020-00724-3), _Modern Pathology_, 2020
- [Rosenberg et al](https://ascopubs.org/doi/10.1200/JCO.2020.38.15_suppl.e23529), _Journal of Clinical Oncology_ [abstract], 2020
- [Howard et al](https://www.nature.com/articles/s41467-021-24698-1), _Nature Communications_, 2021
- [Dolezal et al](https://www.nature.com/articles/s41467-022-34025-x) _Nature Communications_, 2022
- [Storozuk et al](https://www.nature.com/articles/s41379-022-01039-1.pdf), _Modern Pathology_ [abstract], 2022
- [Partin et al](https://doi.org/10.3389/fmed.2023.1058919) _Front Med_, 2022
- [Dolezal et al](https://ascopubs.org/doi/abs/10.1200/JCO.2022.40.16_suppl.8549) _Journal of Clinical Oncology_ [abstract], 2022
- [Dolezal et al](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9792820/) _Mediastinum_ [abstract], 2022
- [Howard et al](https://www.nature.com/articles/s41523-023-00530-5) _npj Breast Cancer_, 2023
- [Dolezal et al](https://www.nature.com/articles/s41698-023-00399-4) _npj Precision Oncology_, 2023
- [Hieromnimon et al](https://doi.org/10.1101/2023.03.22.533810) [bioRxiv], 2023

## License
This code is made available under the GPLv3 License and is available for non-commercial academic purposes.

## Reference
If you find our work useful for your research, or if you use parts of this code, please consider citing as follows:

Dolezal, J. M., Kochanny, S., Dyer, E., *et al*. Slideflow: Deep Learning for Digital Histopathology with Real-Time Whole-Slide Visualization. ArXiv [q-Bio.QM] (2023). http://arxiv.org/abs/2304.04142

```
@misc{dolezal2023slideflow,
      title={Slideflow: Deep Learning for Digital Histopathology with Real-Time Whole-Slide Visualization}, 
      author={James M. Dolezal and Sara Kochanny and Emma Dyer and Andrew Srisuwananukorn and Matteo Sacco and Frederick M. Howard and Anran Li and Prajval Mohan and Alexander T. Pearson},
      year={2023},
      eprint={2304.04142},
      archivePrefix={arXiv},
      primaryClass={q-bio.QM}
}
```
