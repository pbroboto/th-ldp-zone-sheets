import glob, os
Z=r"C:\Users\Prajuab\AppData\Local\Temp\claude\D--dev-pocut\3c571eb2-25c1-44f4-81b1-5614ea9a5c0a\scratchpad\nov69"
def rows(path):
    for line in open(path,encoding="utf-8",newline="").read().split("\n"):
        r=[c.strip() for c in line.split("\t")]
        if len(r)>=4 and r[0]: yield r
base={r[1]:r for r in rows(r"D:\dev\pocut\data\th_ldp.tsv")}
old=r"D:\dev\One Page TH-LDP\zones\zone_overrides_pre_nov69.tsv"
design={r[1]:r[4:7] for r in rows(old) if len(r)>=7}
for r in rows(old): base[r[1]]=r
out=[]
for d in sorted(glob.glob(Z+r"\TH_*")):
    code=os.path.basename(d).replace("TH_","TH.",1)
    pj=open(glob.glob(d+r"\*.PJ4")[0]).read().strip()
    b=base[code]
    out.append("\t".join([b[0],code,b[2],pj]+design.get(code,[])))
open(r"D:\dev\One Page TH-LDP\zones\zone_overrides.tsv","w",encoding="utf-8",newline="").write("\n".join(out)+"\n")
print(len(out))
