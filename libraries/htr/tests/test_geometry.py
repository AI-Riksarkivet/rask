def test_bbox_dimensions():
    from htr.geometry import Bbox

    b = Bbox(10, 20, 110, 70)
    assert b.width == 100
    assert b.height == 50


def test_bbox_to_polygon_has_four_points():
    from htr.geometry import Bbox

    b = Bbox(0, 0, 100, 50)
    poly = b.polygon()
    assert poly is not None
    assert len(poly) == 4
