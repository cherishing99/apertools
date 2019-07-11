#! /usr/bin/env python
"""Author: Scott Staniewicz
Input/Output functions for loading/saving SAR data in binary formats
Email: scott.stanie@utexas.edu
"""

from __future__ import division, print_function
import datetime
import glob
import math
import json
import os
import re
import sys
import numpy as np
import h5py
import matplotlib.pyplot as plt
from PIL import Image
import sardem

from apertools import parsers, utils
from apertools.log import get_log
logger = get_log()

FLOAT_32_LE = np.dtype('<f4')
COMPLEX_64_LE = np.dtype('<c8')

SENTINEL_EXTS = ['.geo', '.cc', '.int', '.amp', '.unw', '.unwflat']
UAVSAR_EXTS = [
    '.int',
    '.mlc',
    '.slc',
    '.amp',
    '.cor',
    '.grd',
    '.unw',
    '.int.grd',
    '.unw.grd',
    '.cor.grd',
]
IMAGE_EXTS = ['.png', '.tif', '.tiff', '.jpg']

# Notes: .grd, .mlc can be either real or complex for UAVSAR,
# .amp files are real only for UAVSAR, complex for sentinel processing
# However, we label them as real here since we can tell .amp files
# are from sentinel if there exists .rsc files in the same dir
COMPLEX_EXTS = [
    '.int',
    '.slc',
    '.geo',
    '.cc',
    '.unw',
    '.unwflat',
    '.mlc',
    '.int.grd',
]
REAL_EXTS = [
    '.amp',
    '.cor',
    '.mlc',
    '.grd',
    '.cor.grd',
]  # NOTE: .cor might only be real for UAVSAR
# Note about UAVSAR Multi-look products:
# Will end with `_ML5X5.grd`, e.g., for 5x5 downsampled

ELEVATION_EXTS = ['.dem', '.hgt']

# These file types are not simple complex matrices: see load_stacked_img for detail
# .unwflat are same as .unw, but with a linear ramp removed
STACKED_FILES = ['.cc', '.unw', '.unwflat', '.unw.grd', '.cc.grd']
# real or complex for these depends on the polarization
UAVSAR_POL_DEPENDENT = ['.grd', '.mlc']

GEOLIST_DSET = "geo_dates"
INTLIST_DSET = "int_dates"


def load_file(filename,
              downsample=None,
              looks=None,
              rsc_file=None,
              ann_info=None,
              verbose=False,
              **kwargs):
    """Examines file type for real/complex and runs appropriate load

    Args:
        filename (str): path to the file to open
        rsc_file (str): path to a dem.rsc file (if Sentinel)
        ann_info (dict): data parsed from annotation file (UAVSAR)
        downsample (int): rate at which to downsample the file
            None is equivalent to 1, no downsampling.
        looks (tuple[int, int]): downsample by taking looks
            None is equivalent to (1, 1), no downsampling.
        verbose (bool): print extra logging info while loading files

    Returns:
        ndarray: a 2D array of the data from a file

    Raises:
        ValueError: if sentinel files loaded without a .rsc file in same path
            to give the file width
    """
    if downsample:
        if (downsample < 1 or not isinstance(downsample, int)):
            raise ValueError("downsample must be a positive integer")
        looks = (downsample, downsample)
    elif looks:
        if any((r < 1 or not isinstance(r, int)) for r in looks):
            raise ValueError("looks values must be a positive integers")
    else:
        looks = (1, 1)

    ext = utils.get_file_ext(filename)
    # Pass through numpy files to np.load
    if ext == '.npy':
        return utils.take_looks(np.load(filename), *looks)

    if ext == '.geojson':
        with open(filename) as f:
            return json.load(f)

    # Elevation and rsc files can be immediately loaded without extra data
    if ext in ELEVATION_EXTS:
        return utils.take_looks(sardem.loading.load_elevation(filename), *looks)
    elif ext == '.rsc':
        return sardem.loading.load_dem_rsc(filename, **kwargs)

    # Sentinel files should have .rsc file: check for dem.rsc, or elevation.rsc
    rsc_data = None
    if rsc_file:
        rsc_data = sardem.loading.load_dem_rsc(rsc_file)

    if ext in IMAGE_EXTS:
        return np.array(Image.open(filename).convert("L"))  # L for luminance == grayscale

    if ext in SENTINEL_EXTS:
        rsc_file = rsc_file if rsc_file else find_rsc_file(filename, verbose=verbose)
        if rsc_file:
            rsc_data = sardem.loading.load_dem_rsc(rsc_file)

    if ext == '.grd':
        ext = _get_full_grd_ext(filename)

    # UAVSAR files have an annotation file for metadata
    if not ann_info and not rsc_data and ext in UAVSAR_EXTS:
        try:
            u = parsers.Uavsar(filename, verbose=verbose)
            ann_info = u.parse_ann_file()
        except ValueError:
            try:
                u = parsers.UavsarInt(filename, verbose=verbose)
                ann_info = u.parse_ann_file()
            except ValueError:
                print("Failed loading ann_info")
                pass

    if not ann_info and not rsc_file:
        raise ValueError("Need .rsc file or .ann file to load")

    if ext in STACKED_FILES:
        stacked = load_stacked_img(filename, rsc_data=rsc_data, ann_info=ann_info, **kwargs)
        return stacked[..., ::downsample, ::downsample]
    # having rsc_data implies that this is not a UAVSAR file, so is complex
    elif rsc_data or is_complex(filename=filename, ext=ext):
        return utils.take_looks(load_complex(filename, ann_info=ann_info, rsc_data=rsc_data),
                                *looks)
    else:
        return utils.take_looks(load_real(filename, ann_info=ann_info, rsc_data=rsc_data), *looks)


# Make a shorter alias for load_file
load = load_file


def _get_full_grd_ext(filename):
    if any(e in filename for e in ('.int', '.unw', '.cor', 'cc')):
        ext = '.' + '.'.join(filename.split('.')[-2:])
        logger.info("Using %s for full grd extension" % ext)
        return ext
    else:
        return '.grd'


def find_files(directory, search_term):
    """Searches for files in `directory` using globbing on search_term

    Path to file is also included.
    Returns in names sorted order.

    Examples:
    >>> import shutil, tempfile
    >>> temp_dir = tempfile.mkdtemp()
    >>> open(os.path.join(temp_dir, "afakefile.txt"), "w").close()
    >>> print('afakefile.txt' in find_files(temp_dir, "*.txt")[0])
    True
    >>> shutil.rmtree(temp_dir)
    """
    return sorted(glob.glob(os.path.join(directory, search_term)))


def find_rsc_file(filename=None, directory=None, verbose=False):
    if filename:
        directory = os.path.split(os.path.abspath(filename))[0]
    # Should be just elevation.dem.rsc (for .geo folder) or dem.rsc (for igrams)
    possible_rscs = find_files(directory, '*.rsc')
    if verbose:
        logger.info("Searching %s for rsc files", directory)
        logger.info("Possible rsc files:")
        logger.info(possible_rscs)
    if len(possible_rscs) < 1:
        logger.info("No .rsc file found in %s", directory)
        return None
        # raise ValueError("{} needs a .rsc file with it for width info.".format(filename))
    elif len(possible_rscs) > 1:
        raise ValueError("{} has multiple .rsc files in its directory: {}".format(
            filename, possible_rscs))
    return utils.fullpath(possible_rscs[0])


def _get_file_rows_cols(ann_info=None, rsc_data=None):
    """Wrapper function to find file width for different SV types"""
    if (not rsc_data and not ann_info) or (rsc_data and ann_info):
        raise ValueError("needs either ann_info or rsc_data (but not both) to find number of cols")
    elif rsc_data:
        return rsc_data['file_length'], rsc_data['width']
    elif ann_info:
        return ann_info['rows'], ann_info['cols']


def _assert_valid_size(data, cols):
    """Make sure the width of the image is valid for the data size

    Note that only width is considered- The number of rows is ignored
    """
    error_str = "Invalid number of cols (%s) for file size %s." % (cols, len(data))
    # math.modf returns (fractional remainder, integer remainder)
    assert math.modf(float(len(data)) / cols)[0] == 0, error_str


def load_real(filename, ann_info=None, rsc_data=None):
    """Reads in real 4-byte per pixel files""

    Valid filetypes: See sario.REAL_EXTS

    Args:
        filename (str): path to the file to open
        rsc_data (dict): output from load_dem_rsc, gives width of file
        ann_info (dict): data parsed from UAVSAR annotation file

    Returns:
        ndarray: float32 values for the real 2D matrix

    """
    data = np.fromfile(filename, FLOAT_32_LE)
    rows, cols = _get_file_rows_cols(ann_info=ann_info, rsc_data=rsc_data)
    _assert_valid_size(data, cols)
    return data.reshape([-1, cols])


def load_complex(filename, ann_info=None, rsc_data=None):
    """Combines real and imaginary values from a filename to make complex image

    Valid filetypes: See sario.COMPLEX_EXTS

    Args:
        filename (str): path to the file to open
        rsc_data (dict): output from load_dem_rsc, gives width of file
        ann_info (dict): data parsed from UAVSAR annotation file

    Returns:
        ndarray: imaginary numbers of the combined floats (dtype('complex64'))
    """
    data = np.fromfile(filename, FLOAT_32_LE)
    rows, cols = _get_file_rows_cols(ann_info=ann_info, rsc_data=rsc_data)
    _assert_valid_size(data, cols)

    real_data, imag_data = parse_complex_data(data, cols)
    return combine_real_imag(real_data, imag_data)


def load_stacked_img(filename, rsc_data=None, ann_info=None, return_amp=False, **kwargs):
    """Helper function to load .unw and .cor files

    Format is two stacked matrices:
        [[first], [second]] where the first "cols" number of floats
        are the first matrix, next "cols" are second, etc.
    For .unw height files, the first is amplitude, second is phase (unwrapped)
    For .cc correlation files, first is amp, second is correlation (0 to 1)

    Args:
        filename (str): path to the file to open
        rsc_data (dict): output from load_dem_rsc, gives width of file
        return_amp (bool): flag to request the amplitude data to be returned

    Returns:
        ndarray: dtype=float32, the second matrix (height, correlation, ...) parsed
        if return_amp == True, returns two ndarrays stacked along axis=0

    Example illustrating how strips of data alternate:
    reading unw (unwrapped phase) data

    data = np.fromfile('20141128_20150503.unw', '<f4')

    # The first section of data is amplitude data
    # The amplitude has a different, larger range of values
    amp = data[:cols]
    print(np.max(amp), np.min(amp))
    # Output: (27140.396, 118.341095)

    # The next part of the data is a line of phases:
    phase = data[cols:2*cols])
    print(np.max(phase), np.min(phase))
    # Output: (8.011558, -2.6779003)
    """
    data = np.fromfile(filename, FLOAT_32_LE)
    rows, cols = _get_file_rows_cols(rsc_data=rsc_data, ann_info=ann_info)
    _assert_valid_size(data, cols)

    first = data.reshape((rows, 2 * cols))[:, :cols]
    second = data.reshape((rows, 2 * cols))[:, cols:]
    if return_amp:
        return np.stack((first, second), axis=0)
    else:
        return second


def is_complex(filename=None, ext=None):
    """Helper to determine if file data is real or complex

    Uses https://uavsar.jpl.nasa.gov/science/documents/polsar-format.html for UAVSAR
    Note: differences between 3 polarizations for .mlc files: half real, half complex
    """
    if ext is None:
        ext = utils.get_file_ext(filename)

    if ext not in COMPLEX_EXTS and ext not in REAL_EXTS:
        raise ValueError('Invalid filetype for load_file: %s\n '
                         'Allowed types: %s' % (ext, ' '.join(COMPLEX_EXTS + REAL_EXTS)))

    if ext in UAVSAR_POL_DEPENDENT:
        # Check if filename has one of the complex polarizations
        return any(pol in filename for pol in parsers.Uavsar.COMPLEX_POLS)
    else:
        return ext in COMPLEX_EXTS


def parse_complex_data(complex_data, cols):
    """Splits a 1-D array of real/imag bytes to 2 square arrays"""
    # double check if I ever need rows
    real_data = complex_data[::2].reshape([-1, cols])
    imag_data = complex_data[1::2].reshape([-1, cols])
    return real_data, imag_data


def combine_real_imag(real_data, imag_data):
    """Combines two float data arrays into one complex64 array"""
    return real_data + 1j * imag_data


def save(filename, array, normalize=True, cmap="gray", preview=False, vmax=None, vmin=None):
    """Save the numpy array in one of known formats

    Args:
        filename (str): Output path to save file in
        array (ndarray): matrix to save
        normalize (bool): scale array to [-1, 1]
        cmap (str, matplotlib.cmap): colormap (if output is png/jpg and will be plotted)
        preview (bool): for png/jpg, display the image before saving
    Returns:
        None

    Raises:
        NotImplementedError: if file extension of filename not a known ext
    """

    def _is_little_endian():
        """All UAVSAR data products save in little endian byte order"""
        return sys.byteorder == 'little'

    def _force_float32(arr):
        if np.issubdtype(arr.dtype, np.floating):
            return arr.astype(FLOAT_32_LE)
        elif np.issubdtype(arr.dtype, np.complexfloating):
            return arr.astype(COMPLEX_64_LE)
        else:
            return arr

    ext = utils.get_file_ext(filename)
    if ext == '.grd':
        ext = _get_full_grd_ext(filename)
    if ext == '.png':  # TODO: or ext == '.jpg':
        # Normalize to be between 0 and 1
        if normalize:
            array = array / np.max(np.abs(array))
            vmin, vmax = -1, 1
        logger.info("previewing with (vmin, vmax) = (%s, %s)" % (vmin, vmax))
        # from PIL import Image
        # im = Image.fromarray(array)
        # im.save(filename)
        if preview:
            plt.imshow(array, cmap=cmap, vmin=vmin, vmax=vmax)
            plt.colorbar()
            plt.show(block=True)

        plt.imsave(filename, array, cmap=cmap, vmin=vmin, vmax=vmax, format=ext.strip('.'))

    elif (ext in COMPLEX_EXTS + REAL_EXTS + ELEVATION_EXTS) and (ext not in STACKED_FILES):
        # If machine order is big endian, need to byteswap (TODO: test on big-endian)
        # TODO: Do we need to do this at all??
        if not _is_little_endian():
            array.byteswap(inplace=True)

        _force_float32(array).tofile(filename)
    elif ext in STACKED_FILES:
        if array.ndim != 3:
            raise ValueError("Need 3D stack ([amp, data]) to save.")
        # first = data.reshape((rows, 2 * cols))[:, :cols]
        # second = data.reshape((rows, 2 * cols))[:, cols:]
        np.hstack((array[0], array[1])).astype(FLOAT_32_LE).tofile(filename)

    else:
        raise NotImplementedError("{} saving not implemented.".format(ext))


def save_hgt(filename, amp_data, height_data):
    save(filename, np.stack((amp_data, height_data), axis=0))


def load_stack(file_list=None, directory=None, file_ext=None, **kwargs):
    """Reads a set of images into a 3D ndarray

    Args:
        file_list (list[str]): list of file names to stack
        directory (str): alternative to file_name: path to a dir containing all files
            This will be loaded in ls-sorted order
        file_ext (str): If using `directory`, the ending type
            of files to read (e.g. '.unw')

    Returns:
        ndarray: 3D array of each file stacked
            1st dim is the index of the image: stack[0, :, :]
    """
    if file_list is None:
        if file_ext is None:
            raise ValueError("need file_ext if using `directory`")
        else:
            file_list = find_files(directory, "*" + file_ext)

    # Test load to get shape
    test = load(file_list[0], **kwargs)
    nrows, ncols = test.shape
    dtype = test.dtype
    out = np.empty((len(file_list), nrows, ncols), dtype=dtype)

    # Now lazily load the files and store in pre-allocated 3D array
    file_gen = (load(filename, **kwargs) for filename in file_list)
    for idx, img in enumerate(file_gen):
        out[idx] = img

    return out


def get_full_path(directory=None, filename=None, full_path=None):
    if full_path:
        directory, filename = os.path.split(full_path)
    else:
        full_path = os.path.join(directory, filename)
    return directory, filename, full_path


def load_deformation(igram_path=".", filename='deformation.npy', full_path=None, n=None):
    """Loads a stack of deformation images from igram_path

    igram_path must also contain the "geolist.npy" file if using the .npy option

    Args:
        igram_path (str): directory of .npy file
        filename (str): default='deformation.npy', a .npy file of a 3D ndarray
        n (int): only load the last `n` layers of the stack

    Returns:
        tuple[ndarray, ndarray]: geolist 1D array, deformation 3D array
    """
    igram_path, filename, full_path = get_full_path(igram_path, filename, full_path)

    if utils.get_file_ext(filename) == ".npy":
        return _load_deformation_npy(igram_path=igram_path,
                                     filename=filename,
                                     full_path=full_path,
                                     n=n)
    elif utils.get_file_ext(filename) in (".h5", "hdf5"):
        return _load_deformation_h5(igram_path=igram_path,
                                    filename=filename,
                                    full_path=full_path,
                                    n=n)
    else:
        raise ValueError("load_deformation only supported for .h5 or .npy")


def _load_deformation_h5(igram_path=None, filename=None, full_path=None, n=None):
    igram_path, filename, full_path = get_full_path(igram_path, filename, full_path)
    try:
        with h5py.File(full_path, "r") as f:
            # TODO: get rid of these strings not as constants
            if n is not None:
                deformation = f["stack"][-n:]
            else:
                deformation = f["stack"][:]
            # geolist attr will be is a list of strings: need them as datetimes

        geolist = load_geolist_from_h5(full_path)
    except (IOError, OSError) as e:
        logger.error("Can't load %s in path %s: %s", filename, igram_path, e)
        return None, None

    return geolist, deformation


def _load_deformation_npy(igram_path=None, filename=None, full_path=None, n=None):
    igram_path, filename, full_path = get_full_path(igram_path, filename, full_path)

    try:
        deformation = np.load(os.path.join(igram_path, filename))
        if n is not None:
            deformation = deformation[-n:]
        # geolist is a list of datetimes: encoding must be bytes
        geolist = np.load(os.path.join(igram_path, 'geolist.npy'), encoding='bytes')
    except (IOError, OSError):
        logger.error("%s or geolist.npy not found in path %s", filename, igram_path)
        return None, None

    return geolist, deformation


def load_geolist_from_h5(h5file):
    with h5py.File(h5file, "r") as f:
        geolist_str = f[GEOLIST_DSET][()].astype(str)

    return parse_geolist_strings(geolist_str)


def load_intlist_from_h5(h5file):
    with h5py.File(h5file, "r") as f:
        date_pair_strs = f[INTLIST_DSET][:].astype(str)

    return parse_intlist_strings(date_pair_strs)


def parse_geolist_strings(geolist_str):
    return [_parse(g) for g in geolist_str]


def parse_intlist_strings(date_pairs):
    return [(_parse(early), _parse(late)) for early, late in date_pairs]


def _parse(datestr):
    return datetime.datetime.strptime(datestr, "%Y%m%d").date()


def find_geos(directory=".", parse=True, filename=None):
    """Reads in the list of .geo files used, in time order

    Can also pass a filename containing .geo files as lines.

    Args:
        directory (str): path to the geolist file or directory
        parse (bool): output as parsed datetime tuples. False returns the filenames
        filename (string): name of a file with .geo filenames

    Returns:
        list[date]: the parse dates of each .geo used, in date order

    """
    if filename is not None:
        with open(filename) as f:
            geo_file_list = f.read().splitlines()
    else:
        geo_file_list = find_files(directory, "*.geo")

    if not parse:
        return geo_file_list

    # Stripped of path for parser
    geolist = [os.path.split(fname)[1] for fname in geo_file_list]
    if not geolist:
        raise ValueError("No .geo files found in %s" % directory)

    if re.match(r'S1[AB]_\d{8}\.geo', geolist[0]):  # S1A_YYYYmmdd.geo
        return sorted([_parse(_strip_geoname(geo)) for geo in geolist])
    elif re.match(r'\d{8}', geolist[0]):  # YYYYmmdd , just a date string
        return sorted([_parse(geo) for geo in geolist])
    else:  # Full sentinel product name
        return sorted([parsers.Sentinel(geo).start_time.date() for geo in geolist])


def _strip_geoname(name):
    """Leaves just date from format S1A_YYYYmmdd.geo"""
    return name.replace('S1A_', '').replace('S1B_', '').replace('.geo', '')


def find_igrams(directory=".", parse=True, filename=None):
    """Reads the list of igrams to return dates of images as a tuple

    Args:
        directory (str): path to the igram directory
        parse (bool): output as parsed datetime tuples. False returns the filenames
        filename (string): name of a file with .geo filenames

    Returns:
        tuple(date, date) of (early, late) dates for all igrams (if parse=True)
            if parse=False: returns list[str], filenames of the igrams

    """
    if filename is not None:
        with open(filename) as f:
            igram_file_list = f.read().splitlines()
    else:
        igram_file_list = find_files(directory, "*.int")

    if parse:
        igram_fnames = [os.path.split(f)[1] for f in igram_file_list]
        date_pairs = [intname.strip('.int').split('_') for intname in igram_fnames]
        return parse_intlist_strings(date_pairs)
    else:
        return igram_file_list


def load_mask(geo_date_list=None,
              perform_mask=True,
              deformation_filename=None,
              mask_filename="masks.h5",
              directory=None):
    # TODO: Dedupe this from the insar one
    if not perform_mask:
        return np.ma.nomask

    if directory is not None:
        _, _, mask_full_path = get_full_path(directory=directory, filename=mask_filename)
    else:
        mask_full_path = mask_filename

    # If they pass a deformation .h5 stack, get only the dates actually used
    # instead of all possible dates stored in the mask stack
    if deformation_filename is not None:
        if directory is not None:
            deformation_filename = os.path.join(directory, deformation_filename)
            geo_date_list = load_geolist_from_h5(deformation_filename)

    # Get the indices of the mask layers that were used in the deformation stack
    all_geo_dates = load_geolist_from_h5(mask_full_path)
    if geo_date_list is None:
        used_bool_arr = np.full(len(all_geo_dates), True)
    else:
        used_bool_arr = np.array([g in geo_date_list for g in all_geo_dates])

    with h5py.File(mask_full_path) as f:
        # Maks a single mask image for any pixel that has a mask
        # Note: not using GEO_MASK_SUM_DSET since we may be sub selecting layers
        geo_mask_dset = "geo"
        stack_mask = np.sum(f[geo_mask_dset][used_bool_arr, :, :], axis=0)
        stack_mask = stack_mask > 0
        return stack_mask
