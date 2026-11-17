"""Swap the person: the bundled graph and how one run fills it in."""

from __future__ import annotations

import json

import pytest

from seam import swap


@pytest.fixture(scope="module")
def graph():
    return swap.load_graph()


def _opts(**kw):
    return swap.Options(**{"video": "seam_swap_a.mp4", "identity": "seam_swap_a.png", "frames": 120,
                           "prefix": "seam_swap_a", **kw})


def test_the_bundled_graph_is_unchanged():
    # load_graph checks the hash; a hand edit (or a line-ending conversion) fails here
    assert swap.load_graph()["31"]["class_type"] == "H3CharacterSwapPrepareVideo"


def test_fill_sets_the_length_on_both_nodes_and_the_resolution_as_text(graph):
    w = swap.fill(graph, _opts(frames=192))  # 8 s: the template's 5.0 would cut it short
    assert w["31"]["inputs"]["resolution"] == "768" and isinstance(w["31"]["inputs"]["resolution"], str)
    assert w["31"]["inputs"]["duration_seconds"] == w["trim"]["inputs"]["duration"] == pytest.approx(192.5 / 24)
    assert w["trim"]["inputs"]["strict_duration"] is False
    assert int(w["31"]["inputs"]["duration_seconds"] * 24) == 192  # the pack floors: no frame lost
    assert w["30"]["inputs"]["file"] == "seam_swap_a.mp4" and w["9"]["inputs"]["image"] == "seam_swap_a.png"


def test_fill_leaves_the_template_alone(graph):
    before = json.dumps(graph, sort_keys=True)
    swap.fill(graph, _opts(point=(10, 20)))
    assert json.dumps(graph, sort_keys=True) == before


def test_no_click_replaces_the_largest_person_without_recovery(graph):
    w = swap.fill(graph, _opts())
    assert w["104"]["inputs"]["selection"] == "largest"
    assert "positive_coords" not in w["103"]["inputs"] and "conditioning" in w["103"]["inputs"]
    assert w["107"]["inputs"]["recover_empty_frames"] is False  # the template has it on
    assert swap.fill(graph, _opts(recover=True))["107"]["inputs"]["recover_empty_frames"] is True


def test_a_click_is_one_target_as_a_json_string(graph):
    w = swap.fill(graph, _opts(point=(368, 500)))
    coords = w["103"]["inputs"]["positive_coords"]
    assert isinstance(coords, str) and json.loads(coords) == [{"x": 368, "y": 500}]
    assert "conditioning" not in w["103"]["inputs"] and "conditioning" not in w["105"]["inputs"]
    assert w["104"]["inputs"]["selection"] == "index" and w["104"]["inputs"]["index"] == 0
    assert w["106"]["inputs"]["object_indices"] == "0"
    with pytest.raises(ValueError):
        swap.fill(graph, _opts(point=(-1, 4)))


def test_no_anchor_and_a_device_a_single_gpu_offers(graph):
    w = swap.fill(graph, _opts())
    assert "anchor_image" not in w and "anchor_fit" not in w
    assert "anchor" not in w["803"]["inputs"] and w["803"]["inputs"]["use_anchor"] is False
    assert w["58"]["inputs"]["device"] == "default"
    # nothing left pointing at a removed node
    ids = set(w)
    links = [v for n in w.values() for v in n["inputs"].values() if isinstance(v, list) and len(v) == 2 and isinstance(v[1], int)]
    assert all(src in ids for src, _ in links)


def test_the_grown_mask_is_saved_as_images(graph):
    w = swap.fill(graph, _opts())
    assert w[swap.MASK_IMAGE] == {"class_type": "MaskToImage", "inputs": {"mask": ["110", 0]}}
    assert w[swap.MASK_SAVE]["class_type"] == "SaveImage" and w[swap.MASK_SAVE]["inputs"]["images"] == [swap.MASK_IMAGE, 0]


def test_names_seed_prompt_and_growth(graph):
    w = swap.fill(graph, _opts(seed=7, prompt="a woman in a red coat", expand=32, tier="480"))
    assert w["801"]["inputs"]["seed"] == 7 and w["801"]["inputs"]["prompt"] == "a woman in a red coat"
    assert w["110"]["inputs"]["expansion_px"] == 32 and w["31"]["inputs"]["resolution"] == "480"
    assert w["803"]["inputs"]["shot_id"] == w["807"]["inputs"]["filename_prefix"] == "seam_swap_a"
    assert swap.fill(graph, _opts())["801"]["inputs"]["prompt"] == graph["801"]["inputs"]["prompt"]


@pytest.mark.parametrize("bad", [{"frames": 0}, {"frames": 241}, {"tier": "1080"}, {"start": -1.0}])
def test_fill_refuses_what_a_run_cannot_take(graph, bad):
    with pytest.raises(ValueError):
        swap.fill(graph, _opts(**bad))


def test_fill_refuses_another_graph(graph):
    other = json.loads(json.dumps(graph))
    other["31"]["class_type"] = "Something"
    with pytest.raises(ValueError, match="not the bundled"):
        swap.fill(other, _opts())


def test_canvas_matches_the_sizes_the_pack_was_seen_using():
    assert swap.canvas("768", 720, 1280) == (736, 1344)
    assert swap.canvas("480", 360, 640) == (512, 928)
    assert swap.canvas("768", 736, 1344) == (736, 1344)  # already on the grid and within budget


def test_a_click_is_mapped_through_the_centre_crop():
    # 720x1280 (0.5625) onto 736x1344 (0.5476): the sides are cropped a little
    x0, y0, w, h = swap.seen_area(720, 1280, 736, 1344)
    assert y0 == 0 and h == 1280 and w < 720 and x0 == round((720 - w) / 2)
    assert swap.prepared_point(360, 640, 720, 1280, "768") == (368, 672)  # the centre stays the centre
    with pytest.raises(ValueError, match="outside"):
        swap.prepared_point(2, 640, 720, 1280, "768")  # in the cropped-away strip
