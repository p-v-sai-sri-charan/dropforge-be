"""Sanitizes SVG markup saved as a design layer before it is stored or ever re-rendered.

Layers of kind "svg" (AI-generated vector graphics), "text" and "badge" are SVG markup, and nothing stops a
client from PUTting one directly to /designs/{id} without ever going through the AI/text/badge generators that
normally produce them - so this treats every non-image layer as untrusted input. It parses the markup (blocking
DTDs/external entities, i.e. XXE) and keeps only a small allowlist of plain drawing elements and attributes:
no <script>, no event-handler attributes, no external references (<image>, <use href=...>, CSS url(), ...) that
could make the layer "phone home" to an attacker's server every time someone - including an admin reviewing an
order - later opens it.
"""
from xml.etree.ElementTree import Element, tostring

from defusedxml.ElementTree import fromstring as _parse_safely

_ALLOWED_TAGS = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan",
    "defs", "lineargradient", "radialgradient", "stop", "clippath", "mask", "pattern", "title",
}
_ALLOWED_ATTRS = {
    "d", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "points",
    "transform", "viewbox", "preserveaspectratio", "fill", "fill-rule", "fill-opacity", "stroke", "stroke-width",
    "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-opacity", "opacity", "offset", "stop-color",
    "stop-opacity", "gradientunits", "gradienttransform", "font-family", "font-size", "font-weight", "font-style",
    "text-anchor", "letter-spacing", "id", "class", "clip-path", "mask", "xmlns", "version",
}


def _clean(el: Element) -> None:
    # SVG names are case-sensitive (viewBox, clipPath, linearGradient, ...), so only the namespace prefix is
    # dropped here - matching against the allowlists below is case-insensitive, but the kept name keeps its
    # original case rather than being forced to lowercase.
    el.tag = el.tag.split("}")[-1]
    for name in list(el.attrib):
        bare = name.split("}")[-1]
        if bare.lower() not in _ALLOWED_ATTRS or bare.lower().startswith("on"):
            del el.attrib[name]
        elif name != bare:
            el.attrib[bare] = el.attrib.pop(name)
    for child in list(el):
        if child.tag.split("}")[-1].lower() not in _ALLOWED_TAGS:
            el.remove(child)
        else:
            _clean(child)


def sanitize_svg(svg: str) -> str:
    """Returns a cleaned copy of `svg` containing only allowlisted drawing elements/attributes.
    Raises ValueError if it isn't parseable SVG, or isn't an <svg> document at all."""
    try:
        root = _parse_safely(svg)
    except Exception as e:
        raise ValueError("Invalid graphic") from e
    if not isinstance(root, Element) or root.tag.split("}")[-1].lower() != "svg":
        raise ValueError("Invalid graphic")
    root.tag = "svg"  # normalize casing on the one tag name whose case is fixed and known
    root.set("xmlns", "http://www.w3.org/2000/svg")  # ElementTree parses-but-never-re-emits this; add it back so
    # the output is a self-contained SVG document again (it is later used standalone: wrapped in a data: URI,
    # handed to a renderer on its own, etc.)
    _clean(root)
    return tostring(root, encoding="unicode")
