import sys, os, traceback
sys.path.insert(0, os.path.dirname(__file__))
import zone_pipeline as zp
from qgis.core import QgsApplication
MARK=os.path.join(os.path.dirname(__file__),"_nov_markers"); os.makedirs(MARK,exist_ok=True)
names=[l.split("\t")[0] for l in open(zp.OVERRIDES,encoding="utf-8",newline="").read().split("\n") if l.strip()]
qgs=QgsApplication([],False); qgs.setPrefixPath(zp.QGIS_PREFIX,True); qgs.initQgis()
for name in names:
    m=os.path.join(MARK,name.replace(" ","_"))
    if os.path.exists(m): continue
    try:
        zone=zp.load_zone(name); zdir=zp.ZONES_DIR+"\\"+zone["slug"]
        bbox=zp.buffered_bbox(zp.extract_boundary(zone,zdir))
        render=zp.build_raster(zone,zdir,bbox); zp.build_vectors(zone,zdir)
        zp.osm_to_gpkg(zone,zdir,zdir+r"\osm_highways.json",zdir+r"\osm_poi.json")
        pdf=zp.build_layout(zone,zdir,render); open(m,"w").write(pdf)
        print("RESULT",name,"->",pdf,flush=True)
    except Exception as e:
        print("RESULT",name,"FAILED:",e,flush=True); traceback.print_exc()
qgs.exitQgis()
