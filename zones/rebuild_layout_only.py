import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import zone_pipeline as zp
from qgis.core import QgsApplication

name = sys.argv[1]
zone = zp.load_zone(name)
zdir = zp.ZONES_DIR + "\\" + zone["slug"]
render_path = zdir + r"\zone_render.tif"

qgs = QgsApplication([], False)
qgs.setPrefixPath(zp.QGIS_PREFIX, True)
qgs.initQgis()
pdf = zp.build_layout(zone, zdir, render_path)
print("->", pdf)
qgs.exitQgis()
