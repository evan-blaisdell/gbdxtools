import uuid
from io import BytesIO
from functools import lru_cache

import numpy as np
import mercantile
import requests
from affine import Affine
from imageio import imread

from gbdxtools.images.meta import GeoDaskImage, DaskMeta
from gbdxtools.rda.util import AffineTransform

from shapely.geometry import mapping, box
from shapely.geometry.base import BaseGeometry
from shapely import ops
import pyproj

@lru_cache(maxsize=128)
def load_url(url):
    user_agent = {'user-agent': 'GBDXtools v0.17.1 contact GBDX-Support@digitalglobe.com'}
    r = requests.get(url, headers=user_agent)
    r.raise_for_status()
    memfile = BytesIO(r.content)
    arr = imread(memfile)
    return np.rollaxis(arr, 2, 0)

class EphemeralImage(Exception):
    pass


def raise_aoi_required():
    raise EphemeralImage("Image subset must be specified before it can be made concrete.")


class TmsMeta(object):
    def __init__(self, url, zoom=18, bounds=None):
        self.zoom_level = zoom
        self._name = "image-{}".format(str(uuid.uuid4()))
        self._url = url

        _first_tile = mercantile.Tile(z=self.zoom_level, x=0, y=0)
        _last_tile = mercantile.Tile(z=self.zoom_level, x=180, y=-85.05)
        g = box(*mercantile.xy_bounds(_first_tile)).union(box(*mercantile.xy_bounds(_last_tile)))

        self._full_bounds = g.bounds

        # TODO: populate rest of fields automatically
        self._tile_size = 256
        self._nbands = 3
        self._dtype = "uint8"
        self.bounds = self._expand_bounds(bounds)

    @property
    def bounds(self):
        if self._bounds is None:
            return self._full_bounds
        return self._bounds

    @bounds.setter
    def bounds(self, obj):
        # TODO: set bounds via shapely or bbox, validation
        self._bounds = obj
        if obj is not None:
            self._urls, self._shape = self._collect_urls(self.bounds)

    @property
    def name(self):
        return self._name

    @property
    def dask(self):
        if self._bounds is None:
            return {self._name: (raise_aoi_required, )}
        else:
            urls, shape = self._collect_urls(self.bounds)
            return {(self._name, 0, y, x): (load_url, url) for (y, x), url in urls.items()}

    @property
    def dtype(self):
        return self._dtype

    @property
    def shape(self):
        if self._bounds is None:
            _tile = mercantile.tile(180, -85.05, self.zoom_level)
            nx = _tile.x * self._tile_size
            ny = _tile.y * self._tile_size
            return tuple([self._nbands] + [ny, nx])
        else:
            return self._shape


    @property
    def chunks(self):
        ''' calculate the chunks to build the Dask graph '''

        bands, y_size, x_size = self.shape

        y_full_chunks = y_size // self._tile_size
        y_remainder = y_size % self._tile_size

        y_chunks = (self._tile_size,) * y_full_chunks
        if y_remainder != 0:
            y_chunks = y_chunks + (y_remainder,)

        x_full_chunks = x_size // self._tile_size
        x_remainder = x_size % self._tile_size

        x_chunks = (self._tile_size,) * x_full_chunks
        if x_remainder != 0:
            x_chunks = x_chunks + (x_remainder,)

        return ((bands,), y_chunks, x_chunks)

    @property
    def __geo_transform__(self):
        west, south, east, north = self.bounds
        tfm = Affine.translation(west, north) * Affine.scale((east - west) / self.shape[2], (south - north) / self.shape[1])
        return AffineTransform(tfm, "EPSG:3857")

    def _collect_urls(self, bounds):
        minx, miny, maxx, maxy = self._tile_coords(bounds)
        urls = {(y - miny, x - minx): self._url.format(z=self.zoom_level, x=x, y=y)
                for y in range(miny, maxy + 1) for x in range(minx, maxx + 1)}
        return urls, (3, self._tile_size * (maxy - miny + 1), self._tile_size * (maxx - minx + 1))

    def _expand_bounds(self, bounds):
        if bounds is None:
            return bounds
        min_tile_x, min_tile_y, max_tile_x, max_tile_y = self._tile_coords(bounds)

        ul = box(*mercantile.xy_bounds(mercantile.Tile(z=self.zoom_level, x=min_tile_x, y=max_tile_y)))
        lr = box(*mercantile.xy_bounds(mercantile.Tile(z=self.zoom_level, x=max_tile_x, y=min_tile_y)))

        return ul.union(lr).bounds

    def _tile_coords(self, bounds):
        """ convert mercator bbox to tile index limits """
        tfm = pyproj.Transformer.from_crs(3857, 4326, always_xy=True)
        bounds = ops.transform(tfm.transform, box(*bounds)).bounds

        # because tiles have a common corner, the tiles that cover a
        # given tile includes the adjacent neighbors.
        # https://github.com/mapbox/mercantile/issues/84#issuecomment-413113791

        west, south, east, north = bounds
        epsilon = 1.0e-10
        if east != west and north != south:
            # 2D bbox
            # shrink the bounds a small amount so that
            # shapes/tiles round trip.
            west += epsilon
            south += epsilon
            east -= epsilon
            north -= epsilon

        params = [west, south, east, north, [self.zoom_level]]
        tile_coords = [(tile.x, tile.y) for tile in mercantile.tiles(*params)]
        xtiles, ytiles = zip(*tile_coords)
        minx = min(xtiles)
        miny = min(ytiles)
        maxx = max(xtiles) 
        maxy = max(ytiles)
        return minx, miny, maxx, maxy


class TmsImage(GeoDaskImage):
    ''' An image built from a user given API TMS tiles

    These images will be subject to the rules and metadata of the source TMS tiles
    
    Instead of an ID the zoom level to use can be specified (default is 18). Changing the zoom level will change the resolution of the image.

    Supports the basic methods shared by Catalog Images such as plot() and geotiff().

    Args:
        url (str): (required) The url of the Tms service to be used in the request (ex: https://earthwatch.digitalglobe.com/earthservice/tmsaccess/tms/1.0.0/DigitalGlobe:ImageryTileService@EPSG:3857@jpg/{z}/{x}/{y}.jpg?connectId=connectid)
        zoom (int): (optional) Zoom level to use as the source if the image, default is 18
        bbox (list): (optional) Bounding box of AOI, if aoi() method is not used.

    Example:
        >>> img = TmsImage(r"https://earthwatch.digitalglobe.com/earthservice/tmsaccess/tms/1.0.0/DigitalGlobe:ImageryTileService@EPSG:3857@jpg/{z}/{x}/{y}.jpg?connectId=", zoom=13, bbox=[-109.84, 43.19, -109.59, 43.34])'''


    _default_proj = "EPSG:3857"

    def __new__(cls, url, zoom=18, **kwargs):
        _tms_meta = TmsMeta(url=url, zoom=zoom, bounds=kwargs.get("bounds"))
        gi = mapping(box(*_tms_meta.bounds))
        gt = _tms_meta.__geo_transform__
        self =  super(TmsImage, cls).__new__(cls, _tms_meta, __geo_transform__ = gt, __geo_interface__ = gi)
        self._base_args = {"url": url, "zoom": zoom}
        self._tms_meta = _tms_meta
        g = self._parse_geoms(**kwargs)
        if g is not None:
            return self[g]
        else:
            return self

    @property
    def __daskmeta__(self):
        return self._tms_meta

    def rgb(self, **kwargs):
        return np.rollaxis(self.read(), 0, 3)

    def aoi(self, **kwargs):
        g = self._parse_geoms(**kwargs)
        return self.__class__(bounds=list(g.bounds), **self._base_args)[g]

    def __getitem__(self, geometry):
        if isinstance(geometry, BaseGeometry) or getattr(geometry, "__geo_interface__", None) is not None:
            if self._tms_meta._bounds is None:
                return self.aoi(geojson=mapping(geometry), from_proj=self.proj)
            image = GeoDaskImage.__getitem__(self, geometry)
            image._tms_meta = self._tms_meta
            return image
        else:
            result = super(TmsImage, self).__getitem__(geometry)
            image = super(TmsImage, self.__class__).__new__(self.__class__, result)
            if all([isinstance(e, slice) for e in geometry]) and len(geometry) == len(self.shape):
                xmin, ymin, xmax, ymax = geometry[2].start, geometry[1].start, geometry[2].stop, geometry[1].stop
                xmin = 0 if xmin is None else xmin
                ymin = 0 if ymin is None else ymin
                xmax = self.shape[2] if xmax is None else xmax
                ymax = self.shape[1] if ymax is None else ymax

                g = ops.transform(self.__geo_transform__.fwd, box(xmin, ymin, xmax, ymax))
                image.__geo_interface__ = mapping(g)
                image.__geo_transform__ = self.__geo_transform__ + (xmin, ymin)
            else:
                image.__geo_interface__ = self.__geo_interface__
                image.__geo_transform__ = self.__geo_transform__
            image._tms_meta = self._tms_meta
            return image
