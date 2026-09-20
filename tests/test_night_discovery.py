"""@brief Cold discovery hints must not hide mechanically plausible LED models."""

import pytest

from kicadgen.cad3d import kicad_models as km
from test_kicad_model_candidates_backend import library, supported


def test_led_metric_hint_discovers_cold_model_without_certifying_identity(library):
    root, add, measured = library
    close = add('LED_SMD', 'LED_0805_2012Metric',
                {'body': [2, 1.2, .6], 'total': [2, 1.2, .6]})
    far = add('LED_SMD', 'LED_2816_7142Metric',
              {'body': [7.1, 4.2, 1], 'total': [7.1, 4.2, 1]})
    add('Resistor_SMD', 'R_0805_2012Metric')
    report = km.model_candidates(supported(kind='led', package='small translucent SMD package',
        length_mm=2.1, width_mm=1.2), library=root)
    assert measured == [close]
    assert far not in measured
    assert report['status'] == 'review_required'
    assert report['selected_path'] is None
    assert report['candidates'][0]['path'] == str(close)


def test_led_name_hint_does_not_replace_real_cad_measurement(library):
    root, add, measured = library
    model = add('LED_SMD', 'LED_0805_2012Metric',
                {'body': [4, 4, 1], 'total': [4, 4, 1]})
    report = km.model_candidates(supported(kind='led', package='',
        length_mm=2, width_mm=1.2), library=root)
    assert measured == [model]
    assert report['status'] == 'unresolved'
    assert report['candidates'] == []


def test_explicit_usb_c_receptacle_discovers_cold_models_not_other_connectors(library):
    root, add, measured = library
    good = add('Connector_USB', 'USB_C_Receptacle_Generic')
    add('Connector_USB', 'USB_C_Plug_Generic')
    add('Connector_USB', 'USB_A_Receptacle_Generic')
    add('Connector_Coaxial', 'Antenna_Generic')
    report = km.model_candidates(supported(kind='connector',
        package='USB Type-C SMD receptacle, exact variant unknown'), library=root)
    assert measured == [good]
    assert report['status'] == 'review_required'


def test_tactile_surface_mount_discovers_unmeasured_switch_not_tht_or_slide(library):
    root, add, measured = library
    good = add('Button_Switch_SMD', 'SW_SPST_GenericVendor')
    add('Button_Switch_THT', 'SW_SPST_GenericVendor')
    add('Button_Switch_SMD', 'SW_DIP_SPSTx01_Slide_GenericVendor')
    report = km.model_candidates(supported(kind='switch',
        package='SMD tactile switch, unknown vendor'), library=root)
    assert measured == [good]
    assert report['status'] == 'review_required'
