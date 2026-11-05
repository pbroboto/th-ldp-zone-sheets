"""Batch-run zone_pipeline for every TH-LDP zone in th_ldp.tsv.

Resumable: skips a zone if output/<slug>_TH-LDP.pdf already exists.
Logs progress to zones/atlas_run.log and a final summary to zones/atlas_summary.json.
"""
import os, sys, json, time, traceback

sys.path.insert(0, os.path.dirname(__file__))
import zone_pipeline as zp

LOG = zp.ZONES_DIR + r"\atlas_run.log"
SUMMARY = zp.ZONES_DIR + r"\atlas_summary.json"


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def list_zone_names():
    with open(zp.TSV, encoding="utf-8", newline="") as f:
        content = f.read()
    names = []
    for line in content.split("\n"):
        if not line:
            continue
        row = [c.strip() for c in line.split("\t")]
        names.append(row[0])
    return names


def main():
    from qgis.core import QgsApplication
    qgs = QgsApplication([], False)
    qgs.setPrefixPath(zp.QGIS_PREFIX, True)
    qgs.initQgis()

    names = list_zone_names()
    log(f"starting atlas run: {len(names)} zones")

    results = {"done": [], "skipped_no_boundary": [], "already_done": [], "failed": []}
    for i, name in enumerate(names, 1):
        out_pdf = zp.OUTPUT_DIR + rf"\{name.replace(' ', '_')}_TH-LDP.pdf"
        if os.path.exists(out_pdf):
            log(f"[{i}/{len(names)}] {name}: already done, skipping")
            results["already_done"].append(name)
            continue
        log(f"[{i}/{len(names)}] {name}: starting")
        try:
            pdf = zp.process_zone(name)
            if pdf is None:
                results["skipped_no_boundary"].append(name)
                log(f"[{i}/{len(names)}] {name}: SKIPPED (no boundary)")
            else:
                results["done"].append(name)
                log(f"[{i}/{len(names)}] {name}: OK -> {pdf}")
        except Exception as e:
            results["failed"].append({"name": name, "error": str(e)})
            log(f"[{i}/{len(names)}] {name}: FAILED: {e}")
            log(traceback.format_exc())
        with open(SUMMARY, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)

    log(f"atlas run complete: done={len(results['done'])} "
        f"already_done={len(results['already_done'])} "
        f"skipped={len(results['skipped_no_boundary'])} "
        f"failed={len(results['failed'])}")
    qgs.exitQgis()


if __name__ == "__main__":
    main()
