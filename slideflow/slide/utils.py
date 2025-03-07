"""Utility functions and constants for slide reading."""

import csv
import io
import numpy as np
import shapely.geometry as sg
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw
from slideflow import errors
from types import SimpleNamespace
from typing import Union, List, Tuple, Optional

# Constants
DEFAULT_JPG_MPP = 1
OPS_LEVEL_COUNT = 'openslide.level-count'
OPS_MPP_X = 'openslide.mpp-x'
OPS_VENDOR = 'openslide.vendor'
OPS_BOUNDS_HEIGHT = 'openslide.bounds-height'
OPS_BOUNDS_WIDTH = 'openslide.bounds-width'
OPS_BOUNDS_X = 'openslide.bounds-x'
OPS_BOUNDS_Y = 'openslide.bounds-y'
TIF_EXIF_KEY_MPP = 65326
OPS_WIDTH = 'width'
OPS_HEIGHT = 'height'
DEFAULT_WHITESPACE_THRESHOLD = 230
DEFAULT_WHITESPACE_FRACTION = 1.0
DEFAULT_GRAYSPACE_THRESHOLD = 0.05
DEFAULT_GRAYSPACE_FRACTION = 0.6
FORCE_CALCULATE_WHITESPACE = -1
FORCE_CALCULATE_GRAYSPACE = -1
ROTATE_90_CLOCKWISE = 1
ROTATE_180_CLOCKWISE = 2
ROTATE_270_CLOCKWISE = 3
FLIP_HORIZONTAL = 4
FLIP_VERTICAL = 5


def OPS_LEVEL_HEIGHT(level: int) -> str:
    return f'openslide.level[{level}].height'


def OPS_LEVEL_WIDTH(level: int) -> str:
    return f'openslide.level[{level}].width'


def OPS_LEVEL_DOWNSAMPLE(level: int) -> str:
    return f'openslide.level[{level}].downsample'


def draw_roi(
    img: Union[np.ndarray, str],
    coords: List[List[int]],
    color: str = 'red',
    linewidth: int = 5
) -> np.ndarray:
    """Draw ROIs on image.

    Args:
        img (Union[np.ndarray, str]): Image.
        coords (List[List[int]]): ROI coordinates.

    Returns:
        np.ndarray: Image as numpy array.
    """
    annPolys = [sg.Polygon(b) for b in coords]
    if isinstance(img, np.ndarray):
        annotated_img = Image.fromarray(img)
    elif isinstance(img, str):
        annotated_img = Image.open(io.BytesIO(img))  # type: ignore
    draw = ImageDraw.Draw(annotated_img)
    for poly in annPolys:
        x, y = poly.exterior.coords.xy
        zipped = list(zip(x.tolist(), y.tolist()))
        draw.line(zipped, joint='curve', fill=color, width=linewidth)
    return np.asarray(annotated_img)


def roi_coords_from_image(
    c: List[int],
    args: SimpleNamespace
) -> Tuple[List[int], List[np.ndarray], List[List[int]]]:
    # Scale ROI according to downsample level
    extract_scale = (args.extract_px / args.full_extract_px)

    # Scale ROI according to image resizing
    resize_scale = (args.tile_px / args.extract_px)

    def proc_ann(ann):
        # Scale to full image size
        coord = ann.coordinates
        # Offset coordinates to extraction window
        coord = np.add(coord, np.array([-1 * c[0], -1 * c[1]]))
        # Rescale according to downsampling and resizing
        coord = np.multiply(coord, (extract_scale * resize_scale))
        return coord

    # Filter out ROIs not in this tile
    coords = []
    ll = np.array([0, 0])
    ur = np.array([args.tile_px, args.tile_px])
    for roi in args.rois:
        coord = proc_ann(roi)
        idx = np.all(np.logical_and(ll <= coord, coord <= ur), axis=1)
        coords_in_tile = coord[idx]
        if len(coords_in_tile) > 3:
            coords += [coords_in_tile]

    # Convert ROI to bounding box that fits within tile
    boxes = []
    yolo_anns = []
    for coord in coords:
        max_vals = np.max(coord, axis=0)
        min_vals = np.min(coord, axis=0)
        max_x = min(max_vals[0], args.tile_px)
        max_y = min(max_vals[1], args.tile_px)
        min_x = max(min_vals[0], 0)
        min_y = max(0, min_vals[1])
        width = (max_x - min_x) / args.tile_px
        height = (max_y - min_y) / args.tile_px
        x_center = ((max_x + min_x) / 2) / args.tile_px
        y_center = ((max_y + min_y) / 2) / args.tile_px
        yolo_anns += [[x_center, y_center, width, height]]
        boxes += [np.array([
            [min_x, min_y],
            [min_x, max_y],
            [max_x, max_y],
            [max_x, min_y]
        ])]
    return coords, boxes, yolo_anns


def xml_to_csv(path: str) -> str:
    """Create a QuPath format CSV ROI file from an ImageScope-format XML.

    ImageScope-formatted XMLs are expected to have "Region" and "Vertex"
    attributes. The "Region" attribute should have an "ID" sub-attribute.

    Args:
        path (str): ImageScope XML ROI file path

    Returns:
        str: Path to new CSV file.

    Raises:
        slideflow.errors.ROIError: If the XML could not be converted.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    new_csv_file = path[:-4] + '.csv'
    required_attributes = ['.//Region', './/Vertex']
    if not all(root.findall(a) for a in required_attributes):
        raise errors.ROIError(
            f"No ROIs found in the XML file {path}. Check that the XML "
            "file attributes are named correctly named in ImageScope "
            "format with 'Region' and 'Vertex' tags."
        )
    with open(new_csv_file, 'w', newline='') as csvfile:
        csvwriter = csv.writer(csvfile)
        csvwriter.writerow(['ROI_name', 'X_base', 'Y_base'])
        for region in root.findall('.//Region'):
            id_tag = region.get('Id')
            if not id_tag:
                raise errors.ROIError(
                    "No ID attribute found for Region. Check xml file and "
                    "ensure it adheres to ImageScope format."
                )
            roi_name = 'ROI_' + str(id_tag)
            vertices = region.findall('.//Vertex')
            if not vertices:
                raise errors.ROIError(
                    "No Vertex found in ROI. Check xml file and ensure it "
                    "adheres to ImageScope format."
                )
            csvwriter.writerows([
                [roi_name, vertex.get('X'), vertex.get('Y')]
                for vertex in vertices
            ])
    return new_csv_file


def _align_to_matrix(im1: np.ndarray, im2: np.ndarray, warp_matrix: np.ndarray) -> np.ndarray:
    """Align an image to a warp matrix."""
    import cv2
    # Use the warpAffine function to apply the transformation
    return cv2.warpAffine(im1, warp_matrix, (im2.shape[1], im2.shape[0]), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP)


def _find_translation_matrix(
    im1: np.ndarray,
    im2: np.ndarray,
    *,
    denoise: bool = True,
    h: float = 30,
    block_size: int = 7,
    search_window: int = 21,
    n_iterations: int = 10000,
    termination_eps = 1e-10,
    warp_matrix: Optional[np.ndarray] = None
) -> np.ndarray:
    """
    Align two images using only scaling and translation.

    :param im1: The image to be aligned.
    :param im2: The reference image.
    :return: Aligned image of im1.
    """
    import cv2

    # Convert images to grayscale
    im1_gray = cv2.cvtColor(im1, cv2.COLOR_BGR2GRAY)
    im2_gray = cv2.cvtColor(im2, cv2.COLOR_BGR2GRAY)

    # De-noising
    if denoise:
        im1_gray = cv2.fastNlMeansDenoising(im1_gray, None, h, block_size, search_window)
        im2_gray = cv2.fastNlMeansDenoising(im2_gray, None, h, block_size, search_window)

    # Transform images to normalize contrast
    im1_gray = cv2.equalizeHist(im1_gray)
    im2_gray = cv2.equalizeHist(im2_gray)

    # Define the motion model
    warp_mode = cv2.MOTION_TRANSLATION

    # Define 2x3 matrix to store the transformation
    if warp_matrix is None:
        warp_matrix = np.eye(2, 3, dtype=np.float32)

    # Set the number of iterations and termination criteria
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, n_iterations, termination_eps)

    # Use findTransformECC to compute the transformation
    _, warp_matrix = cv2.findTransformECC(im2_gray, im1_gray, warp_matrix, warp_mode, criteria)

    return warp_matrix


def align_image(im1: np.ndarray, im2: np.ndarray) -> np.ndarray:
    """
    Align two images using only scaling and translation.

    :param im1: The image to be aligned.
    :param im2: The reference image.
    :return: Aligned image of im1.
    """
    warp_matrix = _find_translation_matrix(im1, im2)
    return _align_to_matrix(im1, im2, warp_matrix)


def align_by_translation(
    im1: np.ndarray,
    im2: np.ndarray,
    round: bool = False,
    calculate_mse: bool = False,
    **kwargs
) -> Union[Union[Tuple[float, float], Tuple[int, int]],
           Tuple[Union[Tuple[float, float], Tuple[int, int]], float]]:
    """
    Find the (x, y) translation that aligns im1 to im2.

    Args:
        im1 (np.ndarray): Target for alignment.
        im2 (np.ndarray): Image to align.
        round (bool): Round to the nearest int. Defaults to False.
        calculate_mse (bool): Return the mean squared error (MSE) of alignment.
            Defaults to False.

    """
    import cv2
    try:
        warp_matrix = _find_translation_matrix(im1, im2, **kwargs)
    except cv2.error:
        raise errors.AlignmentError(
            "Could not align images. Check that the images are the same "
            "size, that they are not rotated or flipped, and that they have "
            "overlapping regions."
        )
    alignment = -warp_matrix[0, 2], -warp_matrix[1, 2]
    if round:
        alignment = (int(np.round(alignment[0])), int(np.round(alignment[1])))

    if calculate_mse:
        aligned_im1 = _align_to_matrix(im1, im2, warp_matrix)
        mse = compute_alignment_mse(aligned_im1, im2)
        return alignment, mse
    else:
        return alignment

def compute_alignment_mse(
    imageA: np.ndarray,
    imageB: np.ndarray,
    flatten: bool = True
) -> float:
    """
    Compute the Mean Squared Error between two images in their overlapping region,
    excluding areas that are black (0, 0, 0) in either image.

    :param imageA: First image.
    :param imageB: Second image.
    :return: Mean Squared Error (MSE) between the images in the valid overlapping region.
    """
    # Remove the alpha channel from both images
    if flatten:
        imageA = imageA[:, :, 0:3]
        imageB = imageB[:, :, 0:3]

    assert imageA.shape == imageB.shape, "Image sizes must match."

    # Create a combined mask where neither of the images is black
    combined_mask = np.logical_not(np.logical_or(imageA == 0, imageB == 0))

    # Compute MSE only for valid regions
    diff = (imageA.astype("float") - imageB.astype("float")) ** 2
    err = np.sum(diff[combined_mask]) / np.sum(combined_mask)

    return err


def alignment_to_list(grid):
    if not isinstance(grid, np.ma.MaskedArray):
        raise ValueError("Input should be a numpy masked array.")

    # Extract valid (unmasked) indices
    valid_indices = np.ma.where(~grid.mask)

    # Get the values from these indices
    valid_values = grid[valid_indices]

    # Stack the indices and values
    result = np.column_stack(valid_indices + (valid_values,))

    return result


def best_fit_plane(points):
    # Ensure the input is a numpy array
    points = np.array(points)

    # 1. Center the data
    centroid = points.mean(axis=0)
    centered_points = points - centroid

    # 2. Compute the covariance matrix
    cov_matrix = np.cov(centered_points, rowvar=False)

    # 3. Compute the eigenvalues and eigenvectors
    eigenvalues, eigenvectors = np.linalg.eigh(cov_matrix)

    # 4. Get the eigenvector corresponding to the smallest eigenvalue
    normal_vector = eigenvectors[:, np.argmin(eigenvalues)]

    # The equation of the plane is `normal_vector . (x - centroid) = 0`
    return centroid, normal_vector


def z_on_plane(x, y, centroid, normal):
    cx, cy, cz = centroid
    nx, ny, nz = normal

    if nz == 0:
        raise ValueError("Normal vector's Z component is zero. Can't compute Z value for the given X, Y.")

    z = cz + (nx * (cx - x) + ny * (cy - y)) / nz
    return z