from importlib.metadata import version

import chameleon_rs
import pytest
from chameleon_rs import exc


def test_native_module_loads():
    assert chameleon_rs.core_version() == version('chameleon-rs')
    assert chameleon_rs.__version__ == version('chameleon-rs')


def test_page_template_renders():
    assert chameleon_rs.PageTemplate('<p>${x}</p>')(x='<') == '<p>&lt;</p>'
    assert chameleon_rs.PageTemplate('<p>${x}</p>').render(x=1) == '<p>1</p>'


def test_internal_errors_are_runtime_errors():
    # A panic must surface as an ordinary Exception (D8), not PyO3's PanicException.
    assert issubclass(exc.InternalError, RuntimeError)
    assert issubclass(exc.UnsupportedError, exc.CompilationError)


def test_dump_lists_bytecode():
    dump = chameleon_rs.PageTemplate('<p tal:condition="x">${x}</p>')._program.dump()
    assert 'jump_if_not' in dump
    assert 'emit' in dump


def test_program_is_collected():
    import gc
    import weakref

    template = chameleon_rs.PageTemplate('<p>${x.y}</p>')
    ref = weakref.ref(template)
    del template
    gc.collect()
    assert ref() is None


def test_render_does_not_mutate_after_error():
    template = chameleon_rs.PageTemplate('<p>${x}</p>')
    with pytest.raises(NameError):
        template()
    assert template(x=1) == '<p>1</p>'
