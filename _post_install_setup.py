from pathlib import Path
from reactpy import html
from reactpy.core import vdom
from subprocess import call
from tethys_cli.cli_helpers import setup_django


def run():
    import reactpy_django

    # Manually update reactpy files to fix bug
    html_file = Path(html.__file__)
    html_file.write_text(
        html_file.read_text().replace(
            '"""An HTML fragment - this element will not appear in the DOM"""',
            'attributes.pop("key", None)',
        )
    )

    vdom_file = Path(vdom.__file__)
    vdom_file.write_text(
        vdom_file.read_text().replace(
            'key = attributes.pop("key", None)', 'key = attributes.get("key", None)'
        )
    )
    try:
        rd_js_file = (
            Path(reactpy_django.__file__).parent
            / "static"
            / "reactpy_django"
            / "client.js"
        )
        rd_js_file.write_text(
            rd_js_file.read_text().replace(
                "else A=q.model.tagName;",
                'else A=q.model.tagName === "" ? U0.Fragment : q.model.tagName;',
            )
        )
    except:
        pass

    try:
        r_js_file = [
            x for x in (Path(html.__file__).parent / "_static" / "assets").rglob("*.js")
        ][0]
        r_js_file.write_text(
            r_js_file.read_text().replace(
                "else te=ee.model.tagName;",
                'else te=ee.model.tagName === "" ? k$2 : ee.model.tagName;',
            )
        )
    except:
        pass
    call(["tethys", "manage", "migrate", "reactpy_django"])


if __name__ == "__main__":
    setup_django()
    run()