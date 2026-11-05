import csv, sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8")
CSV = r"D:\dev\One Page TH-LDP\Province LDP Definition\ProvinceLDP.csv"
SQ = r"D:\dev\One Page TH-LDP\Province LDP Definition\LDP_Province_v2.sqlite"
TSV = r"D:\dev\pocut\data\th_ldp.tsv"
OV = r"D:\dev\One Page TH-LDP\zones\zone_overrides.tsv"

def parse(p):
    return {k: v for k, v in (x.split("=") for x in p.split() if "=" in x)}

def cm_cp_of(kind, pr):
    """What the CSV's CM_CP column should be for a definition: TM -> central
    meridian, LCC -> latitude of origin, as deg:min (rounded to the minute)."""
    v = float(pr["+lon_0"]) if kind == "TM" else float(pr["+lat_0"])
    d = int(v); m = round((v - d) * 60)
    if m == 60: d, m = d + 1, 0
    return f"{d}:{m:02d}"

cur = {}                                    # effective definitions in use now
for path in (TSV, OV):
    for line in open(path, encoding="utf-8", newline="").read().split("\n"):
        r = [c.strip() for c in line.split("\t")]
        if len(r) >= 4 and r[0]:
            cur[r[1]] = (r[0], r[2], parse(r[3]))

sq = {row[0]: row for row in sqlite3.connect(SQ).execute(
    "select Province, LDP, CM_CP, PROJ_String_Decimal, NAME_1 from LDP_Province")}

rows = list(csv.DictReader(open(CSV, encoding="utf-8")))
print(f"CSV rows: {len(rows)}   in use: {len(cur)}   sqlite: {len(sq)}")
csv_codes = {r["HASC_1"] for r in rows}
print("codes in use but not in CSV:", sorted(set(cur) - csv_codes))
print("codes in CSV but not in use:", sorted(csv_codes - set(cur)))

bad = 0
for r in rows:
    code, name, ldp, cmcp = r["HASC_1"], r["NAME_1"], r["LDP"], r["CM_CP"]
    if code not in cur:
        continue
    cname, ckind, cp = cur[code]
    issues = []
    if cname != name: issues.append(f"name '{cname}' vs CSV '{name}'")
    if ckind != ldp: issues.append(f"type {ckind} (in use) vs {ldp} (CSV)")
    mine = cm_cp_of(ckind, cp)
    if mine != cmcp: issues.append(f"CM_CP {mine} (in use) vs {cmcp} (CSV)")
    s = sq.get(code)
    if s:
        if s[1] != ldp: issues.append(f"type {s[1]} (sqlite) vs {ldp} (CSV)")
        if s[2].strip() != cmcp: issues.append(f"CM_CP {s[2]} (sqlite) vs {cmcp} (CSV)")
    else:
        issues.append("not in sqlite")
    if issues:
        bad += 1
        print(f"\n{code} {name}")
        for i in issues: print("   ", i)
print(f"\nzones with any CSV discrepancy: {bad} of {len(rows)}")
