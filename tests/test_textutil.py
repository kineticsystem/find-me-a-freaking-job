from jobfinder.textutil import html_to_text


def test_plain_html_becomes_paragraphs_and_bullets():
    out = html_to_text("<h2>About</h2><p>We are <b>hiring</b>.</p><ul><li>C++</li><li>Qt</li></ul>")
    assert out == "About\n\nWe are hiring.\n\n• C++\n• Qt"


def test_entity_escaped_html_is_stripped_too():
    """Greenhouse and Arbeitnow send the markup escaped: &lt;p&gt;. The stripper
    must unescape before deciding whether there are tags to remove."""
    raw = "&lt;p&gt;Fond&eacute;e en 2014, &lt;strong&gt;Artefact&lt;/strong&gt; &amp;amp; co&lt;/p&gt;"
    out = html_to_text(raw)
    assert "<" not in out and "&" not in out.replace("& co", "")
    assert out == "Fondée en 2014, Artefact & co"


def test_text_without_markup_is_left_alone():
    assert html_to_text("no markup &amp; fine\n\nsecond paragraph") == "no markup & fine\n\nsecond paragraph"
    assert html_to_text("") == ""
