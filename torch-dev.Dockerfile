# syntax=docker/dockerfile:1
FROM pytorch/pytorch:1.11.0-cuda11.3-cudnn8-runtime
RUN apt update

# Install necessary packages
RUN apt update && \
    apt install -y liblapack-dev libblas-dev libgl1-mesa-glx libsm6 libxext6 wget vim g++ pkg-config libglib2.0-dev expat libexpat-dev libexif-dev libtiff-dev libgsf-1-dev openslide-tools libopenjp2-tools libpng-dev libtiff5-dev libjpeg-turbo8-dev libopenslide-dev && \
    sed -i '/^#\sdeb-src /s/^# *//' "/etc/apt/sources.list" && \
    apt update

# Build libvips 8.12 from source [slideflow requires 8.9+, latest deb in Ubuntu 18.04 is 8.4]
RUN apt install build-essential devscripts tmux -y && \
    mkdir libvips && \
    mkdir scripts
WORKDIR "/libvips"
RUN wget https://github.com/libvips/libvips/releases/download/v8.12.2/vips-8.12.2.tar.gz && \
    tar zxf vips-8.12.2.tar.gz
WORKDIR "/libvips/vips-8.12.2"
RUN ./configure && make && make install
ENV LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/local/lib

# Repair pixman
WORKDIR "/scripts"
RUN wget https://raw.githubusercontent.com/jamesdolezal/slideflow/2.0.1/scripts/pixman_repair.sh && \
    chmod +x pixman_repair.sh

# Install missing requirements
RUN pip3 install "jupyterlab>=3" && \
    pip3 install "ipywidgets>=7.6" && \
    pip3 install huggingface_hub && \
    pip3 install "timm<0.9" && \
    pip3 install fastai && \
    pip3 install versioneer && \
    pip3 install cupy-cuda11x

RUN pip install icd10-cm \
    pip install neptune && \
    pip install neptune-fastai \
    pip install nystrom-attention