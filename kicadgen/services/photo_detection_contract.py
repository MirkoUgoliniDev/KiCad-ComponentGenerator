"""@brief Pure, bounded geometry and objectness contracts for native image tiles.

Pixel rectangles use [xmin, ymin, xmax, ymax], with exclusive right/bottom
edges. Tiles describe crops only: no resizing, recognition, inference or I/O.
Candidate IDs are stable for exact geometry within a source image; callers
must scope inventories to that image. Nearby or contained boxes are never merged.
"""

import hashlib
import math


def _integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _valid_id(value):
    return type(value) is int or (type(value) is str and bool(value))


def _numbers(values, length, name):
    if not isinstance(values, (list, tuple)) or len(values) != length:
        raise ValueError(f"{name} must contain {length} numbers")
    try:
        valid = all(type(v) in (int, float) and math.isfinite(v) for v in values)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must contain finite non-boolean numbers")
    return list(values)


def _pair(value, name, minimum):
    pair = [value, value] if type(value) is int else value
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        raise ValueError(f"{name} must be an integer or a width/height pair")
    return [_integer(v, name, minimum) for v in pair]


def _rectangle(box, width, height, name):
    x0, y0, x1, y1 = _numbers(box, 4, name)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError(f"{name} is reversed, empty or out of bounds")
    return [x0, y0, x1, y1]


def _tile(tile):
    if not isinstance(tile, dict) or not isinstance(tile.get("id"), str) or not tile["id"]:
        raise ValueError("tile must have a nonempty string id")
    size = tile.get("image_size")
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        raise ValueError("tile image_size must be [width, height]")
    width, height = [_integer(v, "image_size") for v in size]
    bounds = _rectangle(tile.get("bounds"), width, height, "tile bounds")
    core = _rectangle(tile.get("core"), width, height, "tile core")
    if any(type(v) is not int for v in bounds + core):
        raise ValueError("tile bounds and core must be integer pixels")
    if not (bounds[0] <= core[0] < core[2] <= bounds[2]
            and bounds[1] <= core[1] < core[3] <= bounds[3]):
        raise ValueError("tile core must be contained in tile bounds")
    return width, height, bounds, core


def make_tiles(image_width, image_height, tile_size, overlap, *, max_tiles=4096):
    """@brief Describe overlapping native crops with disjoint ownership cores.

    @param image_width Positive integer source width in pixels.
    @param image_height Positive integer source height in pixels.
    @param tile_size Positive integer or [width, height] crop limit in pixels.
    @param overlap Integer or [x, y] overlap in pixels; each is below tile_size.
    @param max_tiles Positive tile budget, checked before allocating descriptors.
    @return Row-major list of dicts with id, image_size, bounds and core lists.
    @throws ValueError For invalid dimensions, overlap or exceeded budget.
    @note Cores partition the image at integer overlap midpoints. At a shared
    edge the right/bottom core owns the center. Last crops may be smaller.
    Caller chooses native sizes supported by its model; no model is hardcoded.
    """
    width = _integer(image_width, "image_width")
    height = _integer(image_height, "image_height")
    tw, th = _pair(tile_size, "tile_size", 1)
    ox, oy = _pair(overlap, "overlap", 0)
    _integer(max_tiles, "max_tiles")
    if ox >= tw or oy >= th:
        raise ValueError("overlap must be smaller than tile_size on both axes")
    sx, sy = tw - ox, th - oy
    nx = 1 + (max(0, width - tw) + sx - 1) // sx
    ny = 1 + (max(0, height - th) + sy - 1) // sy
    if nx * ny > max_tiles:
        raise ValueError("tile budget exceeded")

    def axis(length, size, step, count):
        spans = [(i * step, min(i * step + size, length)) for i in range(count)]
        cuts = [0] + [(spans[i - 1][1] + spans[i][0]) // 2
                      for i in range(1, count)] + [length]
        return [(a, b, cuts[i], cuts[i + 1]) for i, (a, b) in enumerate(spans)]

    xs, ys = axis(width, tw, sx, nx), axis(height, th, sy, ny)
    return [{"id": f"tile:{width}x{height}:{x0},{y0},{x1},{y1}",
             "image_size": [width, height], "bounds": [x0, y0, x1, y1],
             "core": [cx0, cy0, cx1, cy1]}
            for y0, y1, cy0, cy1 in ys for x0, x1, cx0, cx1 in xs]


def make_core_tiles(image_width, image_height, core_size, halo, *, max_tiles=4096):
    """@brief Expand a nonoverlapping core grid by a native-pixel context halo.

    @param image_width Positive integer image width in pixels.
    @param image_height Positive integer image height in pixels.
    @param core_size Positive integer or [width, height] ownership core size.
    @param halo Nonnegative integer or [x, y] context pixels on EACH core side.
    @param max_tiles Positive tile budget checked before allocating descriptors.
    @return Row-major tile dictionaries with id, image_size, bounds and core.
    @throws ValueError For invalid parameters or exceeded tile budget.
    @note Bounds are clipped to the image. Interior crop size is core_size plus
    twice halo; caller supplies all sizes to fit its native model resolution.
    """
    hx, hy = _pair(halo, "halo", 0)
    tiles = make_tiles(image_width, image_height, core_size, 0, max_tiles=max_tiles)
    for tile in tiles:
        x0, y0, x1, y1 = tile["core"]
        bounds = [max(0, x0 - hx), max(0, y0 - hy),
                  min(image_width, x1 + hx), min(image_height, y1 + hy)]
        tile["bounds"] = bounds
        # Include the core: large halos can give different owners identical crops.
        tile["id"] = (f"tile:{image_width}x{image_height}:"
                      + ",".join(map(str, bounds)) + ":core:"
                      + ",".join(map(str, tile["core"])))
    return tiles


def tile_to_image_box(box, tile, coordinate_format="xyxy_pixels"):
    """@brief Convert one complete tile-local box into full-image pixel geometry.

    @param box Four finite numbers, excluding bool; fractional pixels are allowed.
    @param tile Descriptor produced by make_tiles, with integer pixel bounds.
    @param coordinate_format xyxy_pixels (default) or yxyx_1000, the latter
    meaning [ymin, xmin, ymax, xmax] normalized to 0..1000 within this crop.
    @return New [xmin, ymin, xmax, ymax] list in full-image pixels, unrounded.
    @throws ValueError For invalid format, descriptor, box, or crop truncation.
    @note Touching ANY internal crop edge is conservatively rejected. Touching
    an actual image outer edge is allowed. Nothing is clipped or repaired.
    Pixels must refer to the original native crop, not a resized/padded image.
    """
    width, height, (tx0, ty0, tx1, ty1), _ = _tile(tile)
    tw, th = tx1 - tx0, ty1 - ty0
    values = _numbers(box, 4, "box")
    if coordinate_format == "xyxy_pixels":
        x0, y0, x1, y1 = _rectangle(values, tw, th, "box")
    elif coordinate_format == "yxyx_1000":
        y0, x0, y1, x1 = values
        _rectangle([x0, y0, x1, y1], 1000, 1000, "box")
        x0, x1 = x0 / 1000 * tw, x1 / 1000 * tw
        y0, y1 = y0 / 1000 * th, y1 / 1000 * th
    else:
        raise ValueError("unknown coordinate_format")
    if ((x0 == 0 and tx0 > 0) or (y0 == 0 and ty0 > 0)
            or (x1 == tw and tx1 < width) or (y1 == th and ty1 < height)):
        raise ValueError("box touches an internal tile border (possibly truncated)")
    result = [tx0 + x0, ty0 + y0, tx0 + x1, ty0 + y1]
    return _rectangle(result, width, height, "converted box")


def owns_box(box, tile):
    """@brief Test whether a full-image box center belongs to a tile's core.

    @param box Valid full-image [xmin, ymin, xmax, ymax] pixel rectangle.
    @param tile Descriptor produced by make_tiles.
    @return Boolean center membership in the half-open ownership core.
    @throws ValueError For malformed geometry or tile descriptors.
    @note Ownership alone does not establish visibility; convert/validate the
    tile-local box first. Bodies wider than the overlap may require a larger crop.
    """
    width, height, _, (cx0, cy0, cx1, cy1) = _tile(tile)
    x0, y0, x1, y1 = _rectangle(box, width, height, "box")
    cx, cy = x0 + (x1 - x0) / 2, y0 + (y1 - y0) / 2
    return cx0 <= cx < cx1 and cy0 <= cy < cy1


def build_candidates(tiles, detections, coordinate_format="xyxy_pixels", *,
                     require_ownership=False, max_candidates=10000, max_tiles=4096):
    """@brief Track complete geometry with stable IDs and exact deduplication only.

    @param tiles List of descriptors from one make_tiles or make_core_tiles
    call (or a subset).
    @param detections Dict mapping tile IDs to lists of raw four-number boxes.
    Missing tile keys mean no submitted detections, not verified absence.
    @param coordinate_format xyxy_pixels (default) or yxyx_1000.
    @param require_ownership Boolean, default False to retain complete halo
    observations even when a neighboring owning tile misses the body.
    @param max_candidates Positive budget on ALL submitted boxes, including bad ones.
    @param max_tiles Positive budget on submitted descriptors.
    @return Dict with candidates and rejected lists. Candidates have id, tile_id,
    box, owned and observations (tile_id, index, owned). Exact repeated boxes
    share one ID and retain all observation provenance. Rejected rows have
    tile_id, index (zero-based), reason. Candidates are
    sorted by geometry. IDs depend on image dimensions and exact pixel geometry,
    not reply order, type, package or neighboring candidates.
    @throws ValueError For invalid batch structure, foreign/duplicate tile IDs,
    mixed image dimensions, unknown coordinate format or exceeded budgets.
    @note Invalid individual boxes are reported without losing valid siblings.
    No proximity, overlap, containment or semantic merge is performed. Input
    descriptors must retain the disjoint cores generated by the tile helpers.
    """
    _integer(max_candidates, "max_candidates")
    _integer(max_tiles, "max_tiles")
    if type(require_ownership) is not bool:
        raise ValueError("require_ownership must be boolean")
    if coordinate_format not in ("xyxy_pixels", "yxyx_1000"):
        raise ValueError("unknown coordinate_format")
    if not isinstance(tiles, list) or len(tiles) > max_tiles:
        raise ValueError("invalid tiles or tile budget exceeded")
    if not isinstance(detections, dict) or len(detections) > len(tiles):
        raise ValueError("detections must map known tile IDs to box lists")
    registry = {}
    image_size = None
    for tile in tiles:
        width, height, _, _ = _tile(tile)
        if tile["id"] in registry:
            raise ValueError("duplicate tile id")
        if image_size is not None and image_size != (width, height):
            raise ValueError("tiles must describe the same image dimensions")
        image_size = (width, height)
        registry[tile["id"]] = tile
    count = 0
    for tile_id, boxes in detections.items():
        if tile_id not in registry or not isinstance(boxes, list):
            raise ValueError("foreign tile id or non-list detections")
        count += len(boxes)
        if count > max_candidates:
            raise ValueError("candidate budget exceeded")
    candidates, rejected, seen = [], [], {}
    for tile_id in sorted(detections):
        tile = registry[tile_id]
        for index, raw_box in enumerate(detections[tile_id]):
            try:
                box = tile_to_image_box(raw_box, tile, coordinate_format)
                owned = owns_box(box, tile)
                if require_ownership and not owned:
                    raise ValueError("box center is outside the ownership core")
                # Canonicalize signed zero and equivalent int/float coordinates.
                key = tuple(float(v) if v else 0.0 for v in box)
                observation = {"tile_id": tile_id, "index": index, "owned": owned}
                if key in seen:
                    candidate = seen[key]
                    candidate["observations"].append(observation)
                    if owned and not candidate["owned"]:
                        candidate.update(tile_id=tile_id, owned=True)
                    continue
                payload = f"{image_size[0]}x{image_size[1]}:" + ",".join(v.hex() for v in key)
                candidate_id = "candidate:" + hashlib.sha256(payload.encode("ascii")).hexdigest()
                candidate = {"id": candidate_id, "tile_id": tile_id, "box": list(key),
                             "owned": owned, "observations": [observation]}
                seen[key] = candidate
                candidates.append(candidate)
            except ValueError as exc:
                rejected.append({"tile_id": tile_id, "index": index, "reason": str(exc)})
    candidates.sort(key=lambda item: (item["box"], item["id"]))
    return {"candidates": candidates, "rejected": rejected}


def validate_objectness_reply(expected_ids, reply, *, max_candidates=10000,
                              max_review_rows=20000):
    """@brief Validate objectness votes while retaining every expected candidate.

    @param expected_ids List of unique nonempty strings or integer IDs (not bool).
    No coercion is performed: integer 1 and string "1" are different IDs.
    @param reply Untrusted list of dicts with id, status and optional parent_id
    and evidence (string). Status is mounted_body, attached_part, pcb_feature
    or uncertain. Pass the rows themselves, not a tool-response envelope.
    @param max_candidates Positive budget on expected IDs.
    @param max_review_rows Positive budget on reply rows, including duplicates.
    @return Dict with valid, reviews, errors. Reviews follow expected_ids order
    and contain id, status, parent_id, verified, keep and optional evidence
    copied from a valid vote. No geometry is changed.
    @throws ValueError For invalid expected IDs or invalid configured budgets.
    @note Missing, duplicate (even agreeing), or malformed votes become uncertain
    and keep=True. Foreign/unassignable rows invalidate the entire reply because
    its provenance is ambiguous. Valid independent rows survive missing or bad
    votes for known IDs. valid=False means the batch is not a complete success.
    @note An attached_part may name only a distinct, uniquely verified
    mounted_body in this reply. Without a parent it is retained. Only verified
    pcb_feature or attached_part with a verified parent can have keep=False.
    Unknown fields, including replacement geometry, invalidate the affected vote.
    """
    _integer(max_candidates, "max_candidates")
    _integer(max_review_rows, "max_review_rows")
    if (not isinstance(expected_ids, list) or len(expected_ids) > max_candidates
            or any(not _valid_id(v) for v in expected_ids)
            or len(set(expected_ids)) != len(expected_ids)):
        raise ValueError("expected_ids must be bounded, unique strings or integers (not bool)")
    rows = {cid: {"id": cid, "status": "uncertain", "parent_id": None,
                  "verified": False, "keep": True} for cid in expected_ids}
    errors = []

    def result():
        return {"valid": not errors, "reviews": list(rows.values()), "errors": errors}

    if not isinstance(reply, list) or len(reply) > max_review_rows:
        errors.append("reply must be a list within the review row budget")
        return result()
    grouped = {cid: [] for cid in expected_ids}
    for vote in reply:
        if (not isinstance(vote, dict) or not _valid_id(vote.get("id"))
                or vote["id"] not in grouped):
            errors.append("foreign or unassignable review id")
        else:
            grouped[vote["id"]].append(vote)
    if errors:
        return result()
    statuses = ("mounted_body", "attached_part", "pcb_feature", "uncertain")
    for cid, votes in grouped.items():
        if len(votes) != 1:
            errors.append(f"{cid}: missing or duplicate review")
            continue
        vote = votes[0]
        status, parent = vote.get("status"), vote.get("parent_id")
        if (set(vote) - {"id", "status", "parent_id", "evidence"}
                or not isinstance(status, str) or status not in statuses
                or ("evidence" in vote and not isinstance(vote["evidence"], str))
                or (parent is not None and (not _valid_id(parent)
                                            or parent not in rows or parent == cid))
                or (parent is not None and status != "attached_part")):
            errors.append(f"{cid}: invalid review fields")
            continue
        rows[cid].update(status=status, parent_id=parent, verified=status != "uncertain")
        if "evidence" in vote:
            rows[cid]["evidence"] = vote["evidence"]
    for cid, row in rows.items():
        parent = row["parent_id"]
        if parent is not None and rows[parent]["status"] != "mounted_body":
            errors.append(f"{cid}: parent is not a verified mounted body")
            row.update(status="uncertain", parent_id=None, verified=False)
        row["keep"] = not (row["verified"] and (
            row["status"] == "pcb_feature"
            or (row["status"] == "attached_part" and row["parent_id"] is not None)))
    return result()
