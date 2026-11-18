import re

from app.models.content import Figure, FigureTranslation
from app.models.enums import QuestionStatus, Role
from app.models.media import MediaFile
from tests.conftest import make_question, make_user, sign_in

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _question_with_figure(db, author):
    question = make_question(db, stem="Stem text (Figure 1)")
    question.author_id = author.id
    question.status = QuestionStatus.DRAFT
    figure = Figure(question_id=question.id, order=0, path="figures/9/fig.png", mime_type="image/png")
    db.add(figure)
    db.flush()
    db.add(MediaFile(path=figure.path, mime_type="image/png", size=len(PNG), data=PNG))
    for loc, cap in (("en", "Coagulation cascade"), ("ru", "Каскад"), ("ky", "")):
        db.add(FigureTranslation(figure_id=figure.id, locale=loc, caption=cap or None, alt_text=None))
    db.commit()
    return question, figure


def _open(client, db, question):
    db.expire_all()
    return client.get(f"/creative/questions/{question.id}").text


def test_figures_come_before_statements(client, db):
    author = make_user(db, "figauthor", Role.AUTHOR)
    question, _ = _question_with_figure(db, author)
    sign_in(client, author)
    html = _open(client, db, question)
    assert html.index('id="figures"') < html.index('id="statements"')
    assert html.index('id="stem__') < html.index('id="figures"')


def test_figure_fields_follow_the_language_tabs(client, db):
    author = make_user(db, "figauthor2", Role.AUTHOR)
    question, figure = _question_with_figure(db, author)
    sign_in(client, author)
    html = _open(client, db, question)
    panes = re.findall(
        r'<div class="loc-pane" data-tabs-pane="lang" data-loc="(\w+)"\s*(hidden)?>\s*<div class="field">\s*<label for="fig__',
        html,
    )
    shown = [loc for loc, hidden in panes if not hidden]
    assert len(panes) == 3 and len(shown) == 1  # one language visible at a time
    assert 'name="fig__%d__caption__en"' % figure.id in html


def _max_form_depth(html: str) -> int:
    depth = peak = 0
    for tag in re.findall(r"<form\b|</form>", html):
        depth += 1 if tag.startswith("<form") else -1
        peak = max(peak, depth)
    return peak


def test_upload_and_delete_controls_are_outside_the_page_form(client, db):
    author = make_user(db, "figauthor3", Role.AUTHOR)
    question, figure = _question_with_figure(db, author)
    sign_in(client, author)
    html = _open(client, db, question)
    assert 'id="figure-upload-form"' in html and 'enctype="multipart/form-data"' in html
    assert f'id="figdel-{figure.id}"' in html
    assert _max_form_depth(html) == 1  # forms are never nested
    main = html[html.index('id="question-form"'):]
    first_button = re.search(r"<button[^>]*type=\"submit\"[^>]*>", main).group(0)
    assert "formaction" not in first_button  # Enter saves, it does not add a statement


def test_preview_draws_the_figure(client, db):
    author = make_user(db, "figauthor4", Role.AUTHOR)
    question, figure = _question_with_figure(db, author)
    sign_in(client, author)
    html = _open(client, db, question)
    preview = html[html.rindex('class="question-body"'):]
    assert f'src="/media/{figure.path}"' in preview
    assert "<figure" in preview and "Coagulation cascade" in preview or "Каскад" in preview


def test_one_language_switch_controls_everything(client, db):
    author = make_user(db, "langauthor", Role.AUTHOR)
    question, _ = _question_with_figure(db, author)
    sign_in(client, author)
    html = _open(client, db, question)
    # exactly one set of language buttons, and no per-card ones
    assert html.count('class="loc-tabs"') == 1
    assert 'data-tabs="stem"' not in html and 'data-tabs="st"' not in html
    # question text, figure fields, statements and the preview all follow it
    panes = re.findall(r'data-tabs-pane="lang" data-loc="(\w+)"', html)
    assert set(panes) == {"ru", "ky", "en"}
    assert len(panes) >= 3 * 4  # text + figure + statements + preview, per language
    # one preview per language, only the start language visible
    previews = re.findall(r'<div class="card loc-pane" data-tabs-pane="lang" data-loc="(\w+)"\s*style="[^"]*"\s*(hidden)?', html)
    assert len(previews) == 3 and sum(1 for _, hidden in previews if not hidden) == 1
    # the bar comes before the page form so it can stay at the top
    assert html.index('class="lang-bar"') < html.index('id="question-form"')


def test_preview_is_rendered_in_each_language(client, db):
    author = make_user(db, "langauthor2", Role.AUTHOR)
    question, figure = _question_with_figure(db, author)
    sign_in(client, author)
    html = _open(client, db, question)
    assert html.count("Coagulation cascade") >= 2  # the EN caption: input + preview
    assert "Каскад" in html  # the RU caption is in the RU preview
