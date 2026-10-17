"""@brief Reference crops are described once per PNG, not once per compared body.

rotated_image_similarity and match_orientation decoded every stored example
and described it at four quarter turns for each body of the board. The
descriptors are now cached on the PNG bytes; the scores and votes must stay
identical, and a different PNG must never be served from the cache.
"""
import cv2
import numpy as np

from kicadgen.services import component_image_match as match


def _old_rotated_image_similarity(query_descriptor, image_png):
    """Frozen copy of the previous implementation."""
    try:
        crop = cv2.imdecode(np.frombuffer(image_png, np.uint8), cv2.IMREAD_COLOR)
        if crop is None:
            return 0.0
        return max(match.similarity(query_descriptor, match.describe_crop(np.rot90(crop, k).copy()))
                   for k in range(4))
    except (ValueError, TypeError, cv2.error):
        return 0.0


def _old_match_orientation(query_descriptor, examples):
    """Frozen copy of the previous implementation."""
    unresolved = {"status": "unverified", "rotation_deg": None}
    if not isinstance(query_descriptor, np.ndarray):
        return unresolved
    votes = []
    for example in examples:
        try:
            angle = float(example["model_rotation_deg"])
            if not np.isfinite(angle):
                continue
            crop = cv2.imdecode(np.frombuffer(example["image_png"], np.uint8), cv2.IMREAD_COLOR)
            if crop is None:
                continue
            for k in range(4):
                score = match.similarity(query_descriptor,
                                         match.describe_crop(np.rot90(crop, k).copy()))
                votes.append((score, (angle + k * 90) % 360, int(example["id"]), k * 90))
        except (ValueError, TypeError, KeyError, cv2.error):
            continue
    if not votes:
        return unresolved
    votes.sort(reverse=True)
    score, angle, image_id, delta = votes[0]
    conflicting = [v[0] for v in votes if abs((v[1] - angle + 180) % 360 - 180) > 1]
    margin = score - max(conflicting, default=0.0)
    if score < .85 or margin < .08:
        return dict(unresolved, score=score, margin=margin)
    return {"status": "matched", "rotation_deg": angle, "image_id": image_id,
            "relative_rotation_deg": delta, "score": score, "margin": margin}


def _png(rng, height=60, width=90):
    image = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    image[5:25, 10:40] = 30  # an asymmetric block, so the quarter turns differ
    return cv2.imencode(".png", cv2.GaussianBlur(image, (5, 5), 0))[1].tobytes()


def test_scores_and_votes_are_identical_to_the_uncached_ones():
    rng = np.random.default_rng(5)
    pngs = [_png(rng, int(rng.integers(20, 120)), int(rng.integers(20, 120))) for _ in range(12)]
    pngs += [b"not a png", b""]
    examples = [{"id": i + 1, "image_png": png, "model_rotation_deg": float(rng.choice([0, 90, 270]))}
                for i, png in enumerate(pngs)]
    examples += [{"id": 99, "image_png": None, "model_rotation_deg": 0.0},
                 {"image_png": pngs[0], "model_rotation_deg": 0.0},
                 {"id": 98, "image_png": pngs[1], "model_rotation_deg": float("nan")}]
    queries = [match.describe_crop(cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR))
               for png in pngs[:6]]
    queries.append(np.zeros(144, np.float32))
    for _ in range(2):  # the second pass is served by the cache
        for query in queries:
            for png in pngs + [None]:
                assert (match.rotated_image_similarity(query, png)
                        == _old_rotated_image_similarity(query, png))
            for subset in (examples, examples[:1], examples[2:5], []):
                assert match.match_orientation(query, subset) == _old_match_orientation(query, subset)
    wrong_shape = np.zeros(10, np.float32)
    assert match.rotated_image_similarity(wrong_shape, pngs[0]) == 0.0
    assert (match.match_orientation(wrong_shape, examples)
            == _old_match_orientation(wrong_shape, examples))


def test_each_png_is_decoded_once(monkeypatch):
    rng = np.random.default_rng(9)
    pngs = [_png(rng) for _ in range(5)]
    decodes = []
    original = cv2.imdecode

    def counted(*args, **kwargs):
        decodes.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(match, "_ROTATED", match.OrderedDict())
    monkeypatch.setattr(match.cv2, "imdecode", counted)
    query = match.describe_crop(np.full((50, 50, 3), 90, np.uint8))
    examples = [{"id": i, "image_png": png, "model_rotation_deg": 0.0} for i, png in enumerate(pngs)]
    for _ in range(40):  # forty bodies compared with the same archive
        for png in pngs:
            match.rotated_image_similarity(query, png)
        match.match_orientation(query, examples)
    assert len(decodes) == len(pngs)


def test_a_different_png_is_never_served_from_the_cache():
    """Negative case: one changed pixel gives new descriptors."""
    rng = np.random.default_rng(13)
    png = _png(rng)
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    query = match.describe_crop(image)
    before = match.rotated_image_similarity(query, png)
    edited = image.copy()
    edited[30:50, 50:80] = 255 - edited[30:50, 50:80]
    other = cv2.imencode(".png", edited)[1].tobytes()
    after = match.rotated_image_similarity(query, other)
    assert after == _old_rotated_image_similarity(query, other) != before


def test_the_cache_is_bounded(monkeypatch):
    rng = np.random.default_rng(17)
    monkeypatch.setattr(match, "_ROTATED", match.OrderedDict())
    monkeypatch.setattr(match, "_ROTATED_MAX", 3)
    query = np.ones(144, np.float32)
    for _ in range(6):
        match.rotated_image_similarity(query, _png(rng, 20, 20))
    assert len(match._ROTATED) == 3
