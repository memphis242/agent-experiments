#!/usr/bin/env python3
"""
Cross-check the hand-written CCDD JSON in ccdd/json/ against the compiler's view of the flight software.

Usage:  probe_layout.sh [CFS_ROOT] | check_layout.py [JSON_DIR]

For every message definition whose C type name appears in the probe output, this verifies
  - the MID value equals the probe's MID value,
  - each command code value equals the probe's CC value,
  - header size + packed JSON payload size equals sizeof(message),
  - each probed field offset equals header size + the packed offset implied by the JSON.
Exit status is non-zero on any mismatch. Packing is byte-exact (CTF builds payload types with _pack_ = 1),
so any padding the C compiler inserts shows up here as a size or offset mismatch.
"""
import json
import os
import sys

PRIM = {"uint8": 1, "int8": 1, "char": 1, "bool": 1, "uint16": 2, "int16": 2, "uint32": 4, "int32": 4,
        "uint64": 8, "int64": 8, "float": 4, "double": 8}
HDR = {"cmd": 8, "tlm": 16}   # CFE_MSG_CommandHeader_t, CFE_MSG_TelemetryHeader_t (see probe SIZE lines)


def size_of(param):
    """Packed size in bytes of one JSON parameter, recursing through nested parameters."""
    if param.get("parameters"):
        base = sum(size_of(p) for p in param["parameters"])
    else:
        base = PRIM[param["data_type"]]
    count = int(param.get("array_size") or 0)
    return base * (count if count > 0 else 1)


def offsets(params, prefix=""):
    """Map dotted field name -> packed byte offset within the enclosing parameter list."""
    out, pos = {}, 0
    for p in params:
        name = prefix + p["name"]
        out[name] = pos
        if p.get("parameters"):
            for sub, off in offsets(p["parameters"], name + ".").items():
                out[sub] = pos + off
        pos += size_of(p)
    return out


def main():
    json_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "json")
    probe = {"MID": {}, "CC": {}, "SIZE": {}, "OFFSET": {}}
    for line in sys.stdin:
        kind, name, value = line.split()
        if kind in probe:
            probe[kind][name] = int(value, 0)

    errors, checked = [], 0
    mids_json = {}
    for fname in sorted(os.listdir(json_dir)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(json_dir, fname)) as f:
            doc = json.load(f)
        if isinstance(doc, list):
            for entry in doc:
                for m in entry.get("mids", []):
                    mids_json[m["mid_name"]] = int(m["mid_value"], 0)
            continue
        if "cmd_mid_name" in doc:
            kind, mid = "cmd", doc["cmd_mid_name"]
            variants = [(c["cc_name"], c["cc_value"], c["cc_data_type"], c["cc_parameters"]) for c in doc["cmd_codes"]]
        elif "tlm_mid_name" in doc:
            kind, mid = "tlm", doc["tlm_mid_name"]
            variants = [(None, None, doc["tlm_data_type"], doc["tlm_parameters"])]
        else:
            continue
        for cc_name, cc_value, ctype, params in variants:
            label = "%s/%s" % (fname, cc_name or ctype)
            if cc_name in probe["CC"] and probe["CC"][cc_name] != int(cc_value, 0):
                errors.append("%s: cc_value %s != compiler %d" % (label, cc_value, probe["CC"][cc_name]))
            if ctype not in probe["SIZE"]:
                print("note: %s: no probe data for %s (not size-checked)" % (label, ctype))
                continue
            checked += 1
            payload = sum(size_of(p) for p in params)
            if HDR[kind] + payload != probe["SIZE"][ctype]:
                errors.append("%s: size %d + %d != sizeof(%s) = %d"
                              % (label, HDR[kind], payload, ctype, probe["SIZE"][ctype]))
            offs = offsets(params)
            for key, actual in probe["OFFSET"].items():
                tname, field = key.split(".", 1)
                if tname != ctype:
                    continue
                if field not in offs:
                    errors.append("%s: field %s missing in JSON" % (label, field))
                elif HDR[kind] + offs[field] != actual:
                    errors.append("%s: offset of %s is %d, compiler says %d"
                                  % (label, field, HDR[kind] + offs[field], actual))
    for name, value in mids_json.items():
        if name in probe["MID"] and probe["MID"][name] != value:
            errors.append("mission_mids: %s = 0x%04X, compiler says 0x%04X" % (name, value, probe["MID"][name]))
    for name in probe["MID"]:
        if name not in mids_json:
            errors.append("MID %s is probed but absent from the MID list" % name)
    for e in errors:
        print("MISMATCH: " + e)
    print("%d message definitions size-checked, %d MIDs compared, %d mismatches"
          % (checked, len(mids_json), len(errors)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
