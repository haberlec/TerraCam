KPL/MK

\begindata

PATH_VALUES     = ( '.' )
PATH_SYMBOLS    = ( 'KERNELS' )

KERNELS_TO_LOAD = (
    '$KERNELS/naif0012.tls'
    '$KERNELS/de440s.bsp'
    '$KERNELS/pck00011.tpc'
    '$KERNELS/earth_latest_high_prec.bpc'
)

\begintext

TerraCam SPICE Metakernel

Provides the following SPICE kernels for celestial body tracking:

  naif0012.tls                 Leap Seconds Kernel (LSK)
  de440s.bsp                   Planetary Ephemeris SPK (DE440s, 1849-2150)
  pck00011.tpc                 Planetary Constants Kernel (PCK)
  earth_latest_high_prec.bpc   Earth Orientation (high-precision BPC)

Source: NASA NAIF Generic Kernels
        https://naif.jpl.nasa.gov/pub/naif/generic_kernels/

All paths are relative to the directory containing this metakernel.
