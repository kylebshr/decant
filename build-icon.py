#!/usr/bin/env python3
"""
build-icon.py — assemble an openable .icon bundle from an extracted.json
produced by icon-extract.

Usage:
    build-icon.py <extract-dir> <output.icon>

<extract-dir> is the directory icon-extract wrote (containing extracted.json
plus the per-layer .svg/.png assets). It reconstructs icon.json with the
layer/group hierarchy, blend modes, opacity, translucency, specular, shadow,
blur and per-appearance fill specializations, and copies the layer assets into
<output.icon>/Assets/.
"""
import json, os, re, sys, shutil

# CoreUI stores blend modes as CGBlendMode integers; .icon JSON uses names.
BLEND = {0: "normal", 1: "multiply", 2: "screen", 3: "overlay", 4: "darken",
         5: "lighten", 6: "color-dodge", 7: "color-burn", 8: "soft-light",
         9: "hard-light", 10: "difference", 11: "exclusion", 12: "hue",
         13: "saturation", 14: "color", 15: "luminosity",
         26: "plus-darker", 27: "plus-lighter"}
# shadowStyle integer -> .icon shadow kind (calibrated against a known source icon)
SHADOW = {0: "none", 3: "neutral"}
# specularPlacement integer -> .icon specular location (calibrated: 1=inside, 2=outside)
SPEC_PLACEMENT = {1: "inside", 2: "outside"}
# CGColorSpace name -> .icon color-space token
CS = {"kCGColorSpaceDisplayP3": "display-p3", "kCGColorSpaceSRGB": "srgb",
      "kCGColorSpaceExtendedSRGB": "srgb", "kCGColorSpaceGenericRGB": "srgb",
      "kCGColorSpaceGenericGrayGamma2_2": "gray", "kCGColorSpaceLinearGray": "gray",
      "kCGColorSpaceGenericGray": "gray"}

# Appearance keys emitted by icon-extract -> .icon "appearance" tag (None = default/light)
APPEARANCE_TAGS = [
    ("Default", None), ("NSAppearanceNameAqua", None),
    ("UIAppearanceAny", None), ("UIAppearanceLight", None),
    ("NSAppearanceNameDarkAqua", "dark"), ("UIAppearanceDark", "dark"),
    ("ISAppearanceTintable", "tinted"),
]
# Preferred order for choosing the structural base (light/default appearance).
BASE_PREFERENCE = ["Default", "NSAppearanceNameAqua", "UIAppearanceAny", "UIAppearanceLight"]


def colstr(c):
    if not c:
        return None
    space = CS.get(c["colorspace"], "srgb")
    comp = c["components"]
    # 1-2 component colors are grayscale (value [,alpha]) regardless of space name.
    if space == "gray" or len(comp) < 3:
        v = comp[0]
        a = comp[-1] if len(comp) >= 2 else 1.0
        return "gray:%.5f,%.5f" % (v, a)
    if len(comp) >= 4:
        return "%s:%.5f,%.5f,%.5f,%.5f" % (space, comp[0], comp[1], comp[2], comp[3])
    return "%s:%.5f,%.5f,%.5f,1.00000" % (space, comp[0], comp[1], comp[2])


def gradient_fill(cols):
    """A .icon linear-gradient must have exactly 2 colors. 1 color -> solid;
    >2 colors -> approximate with first+last (actool rejects other counts)."""
    cs = [colstr(c) for c in cols]
    if len(cs) == 1:
        return {"solid": cs[0]}
    if len(cs) == 2:
        return {"linear-gradient": cs}
    return {"linear-gradient": [cs[0], cs[-1]]}


def fill(layer):
    g = layer.get("gradient")
    if g and g.get("colors"):
        return gradient_fill(g["colors"])
    c = layer.get("color")
    if c:
        return {"solid": colstr(c)}
    return None


def basename(layer_name):
    # "AppIcon_Assets/1.light" -> "1.light"
    return layer_name.split("/")[-1]


CANVAS = 1024.0  # icon authoring canvas is 1024x1024 points


def parse_frame(s):
    """'{{x, y}, {w, h}}' -> (x, y, w, h) floats, or None."""
    if not s:
        return None
    nums = re.findall(r"-?\d+(?:\.\d+)?", s)
    if len(nums) < 4:
        return None
    return tuple(float(n) for n in nums[:4])


def position_for(frame_str):
    """Convert a captured layer frame to a .icon position {scale, translation-in-points}.
    Default frame {{0,0},{1024,1024}} -> identity (returns None).
    scale = size/canvas; translation = layer-center offset from canvas center,
    with y measured downward (icon canvas origin is top-left)."""
    f = parse_frame(frame_str)
    if not f:
        return None
    x, y, w, h = f
    scale = round(((w / CANVAS) + (h / CANVAS)) / 2.0, 5)
    cx = (x + w / 2.0) - CANVAS / 2.0
    cy = (y + h / 2.0) - CANVAS / 2.0
    # near-identity -> omit
    if abs(scale - 1.0) < 1e-3 and abs(cx) < 1.0 and abs(cy) < 1.0:
        return None
    return {"scale": scale,
            "translation-in-points": [round(cx, 3), round(cy, 3)]}


def _orientation(entry):
    s = entry.get("start"); e = entry.get("end")
    if not (s and e):
        return None
    return {"start": {"x": round(s[0], 4), "y": round(s[1], 4)},
            "stop": {"x": round(e[0], 4), "y": round(e[1], 4)}}


def _pick(byapp, *prefer):
    """From a {appearanceName: gradient} dict, pick the preferred entry."""
    if not byapp:
        return None
    for k in prefer:
        if byapp.get(k):
            return byapp[k]
    return next(iter(byapp.values()), None)


def _auto_gradient(entry):
    """automatic-gradient fill (single base color + orientation) from a gradient
    entry's top stop — matches the .icon authoring schema."""
    cols = entry.get("colors") or []
    if not cols:
        return None
    f = {"automatic-gradient": colstr(cols[0])}
    o = _orientation(entry)
    if o:
        f["orientation"] = o
    return f


def cf_to_fill(cf):
    """Convert a captured canvasFill (leading stack gradient) into a .icon fill.
    A multi-stop gradient becomes a `linear-gradient` (round-trips exactly); a
    single stop becomes an `automatic-gradient`."""
    if not cf:
        return None
    cols = cf.get("colors") or []
    if not cols:
        return None
    if len(cols) == 1:
        f = {"automatic-gradient": colstr(cols[0])}
    elif len(cols) == 2:
        f = {"linear-gradient": [colstr(c) for c in cols]}
    else:
        f = {"linear-gradient": [colstr(cols[0]), colstr(cols[-1])]}
    o = _orientation(cf)
    if o:
        f["orientation"] = o
    return f


def canvas_fill(d, base_key, other):
    """Top-level fill from per-appearance canvasFills (the leading stack
    gradient), with dark/tinted specializations when they differ."""
    base_cf = (d.get(base_key) or {}).get("canvasFill")
    base_fill = cf_to_fill(base_cf)
    if not base_fill:
        return None
    diffs = []
    for k, tag in other:
        if tag is None:
            continue
        f = cf_to_fill((d.get(k) or {}).get("canvasFill"))
        if f and f != base_fill:
            diffs.append((tag, f))
    if diffs:
        specs = [{"value": dict(base_fill)}]
        for tag, f in diffs:
            specs.append({"appearance": tag, "value": f})
        base_fill = dict(base_fill)
        base_fill["fill-specializations"] = specs
    return base_fill


def index_appearance(appdata):
    """Map (group-index, layer-index) -> layer dict for cross-appearance lookup."""
    m = {}
    if not isinstance(appdata, dict):
        return m
    for gi, g in enumerate(appdata["groups"]):
        if g.get("class") != "CUINamedIconLayerGroup":
            continue
        for li, l in enumerate(g.get("layers", [])):
            m[(gi, li)] = l
    return m


# --- group property values (computed from a raw extracted group dict) ---

def g_blend(g):
    return BLEND.get(g.get("blendMode", 0), "normal")

def g_lighting(g):
    return "individual" if g.get("gathersSpecularByElement") else "combined"

def g_specular(g):
    if not g.get("hasSpecular"):
        return False
    return SPEC_PLACEMENT.get(g.get("specularPlacement", 0), True)

def g_translucency(g):
    return {"enabled": g.get("translucency", 1) < 1,
            "value": round(g.get("translucency", 1), 4)}

def g_opacity(g):
    return round(g.get("opacity", 1), 4)

def g_blur(g):
    return round(g["blurStrength"], 4) if g.get("blurStrength", 0) else None

def g_refractivity(g):
    s = round(g.get("refractionStrength", 0) or 0, 4)
    h = round(g.get("refractionHeight", 0) or 0, 4)
    return {"enabled": True, "strength": s, "depth": h} if (s or h) else None


def specialize(grp, key, value_fn, base_g, peers):
    """Emit `key` on grp as a scalar, or as `key-specializations` if the value
    differs across appearances. `peers` is a list of (tag, group_dict). A value
    of None means 'property absent' for that appearance."""
    base_val = value_fn(base_g)
    diffs = []
    for tag, og in peers:
        if og is None:
            continue
        v = value_fn(og)
        if v != base_val:
            diffs.append((tag, v))
    if not diffs:
        if base_val is not None:
            grp[key] = base_val
        return
    specs = []
    if base_val is not None:
        specs.append({"value": base_val})
    for tag, v in diffs:
        if v is not None:
            specs.append({"appearance": tag, "value": v})
    if len(specs) >= 2:
        grp[key + "-specializations"] = specs
    elif base_val is not None:
        grp[key] = base_val


def group_list(appdata):
    """Ordered list of real icon-layer groups for an appearance."""
    if not isinstance(appdata, dict):
        return []
    return [g for g in appdata["groups"] if g.get("class") == "CUINamedIconLayerGroup"]


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    extract_dir, out = sys.argv[1], sys.argv[2]
    d = json.load(open(os.path.join(extract_dir, "extracted.json")))

    # Pick the default (light) appearance as the structural base.
    base_key = next((k for k in BASE_PREFERENCE if isinstance(d.get(k), dict)), None)
    if base_key is None:
        base_key = next(k for k, v in d.items()
                        if isinstance(v, dict) and "groups" in v)
    base = d[base_key]
    other = [(k, tag) for k, tag in APPEARANCE_TAGS
             if k != base_key and isinstance(d.get(k), dict)]
    idx = {k: index_appearance(d[k]) for k, _ in other}
    # Per-appearance group lists, aligned by real-group ordinal, for group-level
    # specializations (blend-mode, specular, translucency, etc. that vary by mode).
    other_groups = [(tag, group_list(d[k])) for k, tag in other if tag is not None]
    base_rgi = -1  # ordinal among real (non-placeholder) base groups

    os.makedirs(os.path.join(out, "Assets"), exist_ok=True)
    groups_out = []
    bottom_fill = None
    uses_refraction = False
    uses_specular_location = False

    for gi, g in enumerate(base["groups"]):
        if g.get("class") != "CUINamedIconLayerGroup":
            continue  # skip top-level gradient/color placeholder entries
        base_rgi += 1
        # aligned peer groups in other appearances (by real-group ordinal)
        peers = [(tag, (glist[base_rgi] if base_rgi < len(glist) else None))
                 for tag, glist in other_groups]
        layers_out = []
        for li, l in enumerate(g.get("layers", [])):
            saved = l.get("savedSVG") or l.get("savedImage")
            if not saved:
                continue
            ext = saved.rsplit(".", 1)[1]
            clean = basename(l["name"]) + "." + ext
            shutil.copy(os.path.join(extract_dir, saved),
                        os.path.join(out, "Assets", clean))
            f0 = fill(l)
            if f0 and bottom_fill is None:
                bottom_fill = f0  # first fill in stack order = bottom-most (backmost) layer
            # appearance-specific fill overrides (vs the base/light fill)
            overrides = []
            for k, tag in other:
                la = idx[k].get((gi, li))
                if la is None:
                    continue
                fa = fill(la)
                if fa and fa != f0 and tag is not None:
                    overrides.append({"appearance": tag, "value": fa})
            layer = {"image-name": clean, "name": basename(l["name"])}
            pos = position_for(l.get("frame"))
            if pos:
                layer["position"] = pos
            # Fill emission. A base fill of None means "no override — use the
            # artwork's own colors" (e.g. an SVG with its own gradient). In that
            # case only the appearance overrides go into fill-specializations,
            # WITHOUT a base value entry — otherwise an appearance-only override
            # (like tinted's gray) would wrongly recolor light/dark too.
            if f0 is None:
                if overrides:
                    layer["fill-specializations"] = overrides
            elif overrides:
                layer["fill-specializations"] = [{"value": f0}] + overrides
            else:
                layer["fill"] = f0

            # Layer properties that can vary per appearance. Emitting these as
            # `<key>-specializations` is essential: e.g. App Store's "1.light"
            # layer has opacity 0 in light but 0.89 in dark — without the dark
            # specialization actool drops the (base-0-opacity) layer entirely.
            def peer(tag_key):
                return idx[tag_key].get((gi, li))

            def lspecialize(key, value_fn, *, omit_if_default=None):
                base_v = value_fn(l)
                diffs = []
                for k, tag in other:
                    if tag is None:
                        continue
                    la = peer(k)
                    if la is None:
                        continue
                    v = value_fn(la)
                    if v != base_v:
                        diffs.append((tag, v))
                if diffs:
                    arr = [{"value": base_v}] + [{"appearance": t, "value": v} for t, v in diffs]
                    layer[key + "-specializations"] = arr
                elif omit_if_default is None or base_v != omit_if_default:
                    layer[key] = base_v

            lspecialize("blend-mode", lambda x: BLEND.get(x.get("blendMode", 0), "normal"),
                        omit_if_default="normal")
            lspecialize("opacity", lambda x: round(x.get("opacity", 1), 4),
                        omit_if_default=1)
            # glass (== hasLightingEffects): refraction/specular only render on
            # glass layers, so this is always emitted.
            lspecialize("glass", lambda x: bool(x.get("hasLightingEffects")))
            layers_out.append(layer)

        # The compiled stack stores layers back-to-front; .icon lists them
        # front-to-back, so reverse within the group.
        layers_out.reverse()

        grp = {"hidden": False, "layers": layers_out,
               "shadow": {"kind": SHADOW.get(g.get("shadowStyle", 0), "neutral"),
                          "opacity": round(g.get("shadowOpacity", 0.5), 4)}}
        # Group properties that can vary per appearance -> emit a scalar, or a
        # `<key>-specializations` array when light/dark/tinted differ.
        specialize(grp, "blend-mode", g_blend, g, peers)
        specialize(grp, "lighting", g_lighting, g, peers)
        specialize(grp, "specular", g_specular, g, peers)
        specialize(grp, "translucency", g_translucency, g, peers)
        specialize(grp, "refractivity", g_refractivity, g, peers)
        # opacity / blur only when non-default (still appearance-aware)
        if g_opacity(g) != 1 or any(og and g_opacity(og) != 1 for _, og in peers):
            specialize(grp, "opacity", g_opacity, g, peers)
        if g_blur(g) is not None or any(og and g_blur(og) is not None for _, og in peers):
            specialize(grp, "blur-material", g_blur, g, peers)

        # feature flags (consider all appearances)
        if g_refractivity(g) or any(og and g_refractivity(og) for _, og in peers):
            uses_refraction = True
        if g_specular(g) in ("inside", "outside") or \
           any(og and g_specular(og) in ("inside", "outside") for _, og in peers):
            uses_specular_location = True
        groups_out.append(grp)

    # The compiled stack stores groups back-to-front; .icon lists them
    # front-to-back, so reverse the group order.
    groups_out.reverse()

    # Top-level canvas fill. Prefer the icon's authored background gradient
    # ("system-light"/"system-dark" named gradients) when present; otherwise
    # fall back to deriving from the bottom-most layer's fill.
    top_fill = canvas_fill(d, base_key, other)
    if top_fill is None:
        if bottom_fill and "solid" in bottom_fill:
            top_fill = {"automatic-gradient": bottom_fill["solid"]}
        elif bottom_fill and "linear-gradient" in bottom_fill:
            top_fill = {"automatic-gradient": bottom_fill["linear-gradient"][-1]}
        else:
            top_fill = {"automatic-gradient": "srgb:0.50000,0.50000,0.50000,1.00000"}

    features = []
    if uses_refraction:
        features.append("refractivity")
    if uses_specular_location:
        features.append("specular-location")

    icon = {}
    if features:
        icon["features"] = features
    icon["fill"] = top_fill
    icon["groups"] = groups_out
    icon["supported-platforms"] = {"circles": ["watchOS"], "squares": "shared"}
    json.dump(icon, open(os.path.join(out, "icon.json"), "w"), indent=2)
    nlayers = sum(len(g["layers"]) for g in groups_out)
    print("wrote %s  (%d groups, %d layers)" % (out, len(groups_out), nlayers))


if __name__ == "__main__":
    main()
