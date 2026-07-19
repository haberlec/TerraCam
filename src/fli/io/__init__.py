"""
FLI I/O Subpackage

Provides file format writers for multispectral image data.
Currently supports NetCDF4 output (requires optional netCDF4 dependency).
"""

try:
    from .netcdf import MultispectralNetCDF
except ImportError:
    # netCDF4 not installed; NetCDF output unavailable
    MultispectralNetCDF = None

__all__ = ['MultispectralNetCDF']
