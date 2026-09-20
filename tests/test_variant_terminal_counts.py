"""@brief Package-family modifiers must not hide a contradictory terminal count."""
from pathlib import Path
import pytest
from kicadgen.cad3d.kicad_models import _declared_count, _compatible_candidates
from kicadgen.cad3d import kicad_models as km
from test_kicad_model_candidates_backend import library, supported


@pytest.mark.parametrize('name,count', [
    ('Nordic_AQFN-73-1EP_7x7mm_P0.5mm',73),
    ('TQFN-44-1EP_7x7mm',44), ('Texas_RGZ0048A_VQFN-48-1EP_7x7mm',48),
    ('VFBGA-98_7x7mm',98), ('TDSON-8-1',8), ('ETSSOP-20-1EP_4x6mm',20),
    ('NXP_SOT1982-1',None), ('SOT-23',None)])
def test_variant_count(name,count):
    assert _declared_count(Path(name+'.step')) == count


def test_four_observed_terminals_exclude_modified_large_packages():
    paths=[Path('Package_DFN_QFN.3dshapes')/(name+'.step')
           for name in ('Nordic_AQFN-73-1EP_7x7mm','TQFN-44-1EP_7x7mm','VQFN-48-1EP_7x7mm')]
    body=dict(kind='ic', pin_count=4, pin_count_confidence='medium')
    assert _compatible_candidates(paths,body,lambda message:None)==[]


def test_cold_discrete_package_without_dimensions_is_measured(library, monkeypatch):
    root, add, measured = library
    path = add('Package_TO_SOT_SMD','SOT-223',{'total':[7,6,1.5],'body':[6,3.5,1.5]})
    monkeypatch.setattr(km,'load_index',lambda *args:{})
    c=supported(kind='ic', package='', length_mm=7, width_mm=6,
                bbox_basis='leads', pin_count=4, terminal_count=4,
                **{'class':'semiconductor_unknown'})
    report=km.model_candidates(c,library=root)
    assert path in measured
    assert report['candidates'][0]['path']==str(path)
    assert report['status']=='review_required'


def test_unreadable_ic_topology_cannot_create_automatic_uniqueness(library, monkeypatch):
    root, add, _=library
    size={'body':[3,3,1],'total':[3,3,1]}
    known=add('Package_DFN_QFN','QFN-4_3x3mm',size)
    unknown=add('Package_DFN_QFN','VendorMystery_3x3mm',size)
    monkeypatch.setattr(km,'load_index',lambda *args:{km.index_key(p):size for p in (known,unknown)})
    c=supported(kind='ic',package='',length_mm=3,width_mm=3,pin_count=4,terminal_count=4)
    report=km.model_candidates(c,library=root)
    assert report['topology_incomplete'] is True
    assert report['status']=='review_required'
    assert [x['path'] for x in report['candidates']]==[str(known)]
