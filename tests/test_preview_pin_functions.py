"""Documented functions remain distinct from electrical pad numbers."""
from kicadgen.core.package_geometry import PackageGeometry, PinSpec


def test_vertical_functions_follow_pin_ids_and_stay_inside_rows(window):
    window.pinout = [{'pin': 2, 'name': 'GPIO43 / U0TXD'}, {'pin': '1', 'name': 'GND'}]
    package = PackageGeometry(name='Board', family='module', body_length_mm=30,
        body_width_mm=20, pins=[
            PinSpec(number='1', x_mm=-5, y_mm=-8, pad_length_mm=1.3,
                    pad_width_mm=1.3, drill_mm=.5, pad_type='thru_hole',
                    shape='circle', side='top'),
            PinSpec(number='2', x_mm=-5, y_mm=8, pad_length_mm=1.3,
                    pad_width_mm=1.3, drill_mm=.5, pad_type='thru_hole',
                    shape='circle', side='bottom'),
            PinSpec(number='3', x_mm=5, y_mm=-8, pad_length_mm=1.3,
                    pad_width_mm=1.3, drill_mm=.5, pad_type='thru_hole',
                    shape='circle', side='top')])
    window._draw_preview_package(package)
    functions = {item.data(1): item for item in window.preview_scene.items()
                 if item.data(0) == 'pin-function'}
    assert set(functions) == {'1', '2'}
    assert functions['1'].text() == '(GND)'
    assert functions['2'].text() == '(GPIO43 / U0TXD)'
    assert all(item.rotation() == 90 for item in functions.values())
    assert not functions['1'].sceneBoundingRect().intersects(functions['2'].sceneBoundingRect())
    assert [pin.number for pin in package.pins] == ['1', '2', '3']
