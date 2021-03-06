"""
GBDX Template Image Interface.

Contact: marc.pfister@maxar.com
"""
from gbdxtools.images.rda_image import RDAImage, GraphMeta
from gbdxtools.rda.graph import get_rda_graph_template, get_rda_template_metadata, VIRTUAL_RDA_URL, get_template_stats, \
    create_rda_template, materialize_status, materialize_template
from gbdxtools.auth import Auth
from gbdxtools.rda.fetch import load_url
from gbdxtools.rda.util import RDA_TO_DTYPE
from shapely.geometry import box
from urllib.parse import urlencode


class TemplateMeta(GraphMeta):
    def __init__(self, name, node_id=None, **kwargs):
        self._template_name = name
        self._template_id = None
        self._rda_id = None
        self._params = kwargs
        self._node_id = node_id
        self._interface = Auth()
        self._rda_meta = None
        self._rda_stats = None
        self._graph = None
        self._nid = None

    @property
    def metadata(self):
        assert self.graph() is not None
        if self._rda_meta is not None:
            return self._rda_meta
        if self._interface is not None:
            self._rda_meta = get_rda_template_metadata(self._interface.gbdx_connection, self._template_id,
                                                       **self._params)
        return self._rda_meta

    def graph(self):
        if self._graph is None:
            template = get_rda_graph_template(self._interface.gbdx_connection, self._template_name)
            self._graph = template
            self._template_id = template.get("id")
        return self._graph

    def _collect_urls(self):
        img_md = self.metadata["image"]
        rda_id = self._template_id
        qs = urlencode(self._params)
        return {(y, x): f"{VIRTUAL_RDA_URL}/template/{rda_id}/tile/{x}/{y}?{qs}"
                for y in range(img_md['minTileY'], img_md["maxTileY"] + 1)
                for x in range(img_md['minTileX'], img_md["maxTileX"] + 1)}

    @property
    def dask(self):
        _name = self.name
        img_md = self.metadata["image"]
        return {(_name, 0, y - img_md['minTileY'], x - img_md['minTileX']): (load_url, url)
                for (y, x), url in self._collect_urls().items()}

    @property
    def name(self):
        return "image-{}".format(self._id)

    @property
    def chunks(self):
        ''' build the chunks
            chunks is tuple of tuples in each dimension
            where the elements are the chunk size '''

        img_md = self.metadata["image"]
        bands, y_size, x_size = self.shape

        y_full_chunks = y_size // img_md["tileYSize"]
        y_remainder = y_size % img_md["tileYSize"]

        y_chunks = (img_md["tileYSize"],) * y_full_chunks
        if y_remainder != 0:
            y_chunks = y_chunks + (y_remainder,)

        x_full_chunks = x_size // img_md["tileXSize"]
        x_remainder = x_size % img_md["tileXSize"]

        x_chunks = (img_md["tileXSize"],) * x_full_chunks
        if x_remainder != 0:
            x_chunks = x_chunks + (x_remainder,)

        return ((bands,), y_chunks, x_chunks)

    @property
    def dtype(self):
        try:
            data_type = self.metadata["image"]["dataType"]
            return RDA_TO_DTYPE[data_type]
        except KeyError:
            raise TypeError("Metadata indicates an unrecognized data type: {}".format(data_type))

    @property
    def shape(self):
        img_md = self.metadata["image"]
        return (img_md["numBands"],
                (img_md["maxTileY"] - img_md["minTileY"] + 1) * img_md["tileYSize"],
                (img_md["maxTileX"] - img_md["minTileX"] + 1) * img_md["tileXSize"])

    def _materialize(self, node=None, bounds=None, callback=None, out_format='TIF', **kwargs):
        conn = self._interface.gbdx_connection
        graph = self.graph()
        graph.pop("id")
        template_id = create_rda_template(conn, graph)
        if node is None:
            node = graph['nodes'][0]['id']
        payload = self._create_materialize_payload(template_id, node, bounds, callback, out_format, **kwargs)
        return materialize_template(conn, payload)

    def _materialize_status(self, job_id):
        return materialize_status(self._interface.gbdx_connection, job_id)

    def _create_materialize_payload(self, templateId, node, bounds, callback, out_format, **kwargs):
        payload = {
            "imageReference": {
                "templateId": templateId,
                "nodeId": node,
                "parameters": kwargs
            },
            "outputFormat": out_format
        }
        if bounds is not None:
            payload["cropGeometryWKT"] = box(*bounds).wkt
        if callback is not None:
            payload["callbackUrl"] = callback
        return payload


class RDATemplateImage(object):
    '''Creates an image instance matching the template name with the given params.

    Args:
        name (str): The RDA template name or ID
        node_id (str): the node ID to render as the image (defaults to None) 
        kwargs: Parameters needed to fill the template params

    Returns:
        image (ndarray): An image instance
    '''

    def __new__(cls, name, node_id=None, **kwargs):
        if node_id and 'nodeId' not in kwargs:
            kwargs['nodeId'] = node_id
        return RDAImage(TemplateMeta(name, node_id, **kwargs))