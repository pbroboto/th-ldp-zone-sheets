import sys, os, traceback
sys.path.insert(0, os.path.dirname(__file__))
import zone_pipeline as zp
from qgis.core import QgsApplication
names=[l.split("\t")[0] for l in open(zp.OVERRIDES,encoding="utf-8",newline="").read().split("\n") if l.strip()]
qgs=QgsApplication([],False); qgs.setPrefixPath(zp.QGIS_PREFIX,True); qgs.initQgis()
for name in names:
    try:
        zone=zp.load_zone(name); zdir=zp.ZONES_DIR+"\\"+zone["slug"]
        print("RESULT",name,"->",zp.build_layout(zone,zdir,zdir+r"\zone_render.tif"),flush=True)
    except Exception as e:
        print("RESULT",name,"FAILED:",e,flush=True); traceback.print_exc()
qgs.exitQgis()
