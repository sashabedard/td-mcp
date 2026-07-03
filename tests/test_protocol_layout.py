from td_mcp.protocol import AnnotationSpec, LayoutDiff


def test_layout_diff_constructs_empty():
    d = LayoutDiff()
    assert d.moved == []
    assert d.checkpoint_id == ""


def test_annotation_spec_requires_bbox():
    a = AnnotationSpec(
        cluster_name="Audio reactive",
        member_paths=["/project1/audiofilein1", "/project1/analyze1"],
        bbox_x=0, bbox_y=0, bbox_w=400, bbox_h=200,
    )
    assert a.bbox_w == 400
