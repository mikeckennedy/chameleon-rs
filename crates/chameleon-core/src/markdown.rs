//! Markdown to HTML for the `markdown:` expression type (Phase 11b): `CommonMark` plus the GitHub
//! extensions, through comrak. A [`Profile`] is built once, when the application configures
//! it; [`Profile::to_html`] converts one value at render time and reads nothing else.

use std::cell::Cell;
use std::fmt;
use std::rc::Rc;

use comrak::html::{ChildRendering, Context, format_document_with_formatter, format_node_default};
use comrak::nodes::{AstNode, NodeValue};
use comrak::options::Plugins;
use comrak::{Arena, Options, parse_document};

/// What happens to HTML written in the Markdown source.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RawHtml {
    /// Shown as text: `<b>` renders as `&lt;b&gt;`. The default, safe for untrusted text.
    Escape,
    /// Passed through as markup. Trusted content only.
    Allow,
}

/// An element `classes` can name, and the Markdown nodes that render it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Element {
    P,
    H(u8),
    Blockquote,
    Ul,
    Ol,
    Li,
    Pre,
    Code,
    Table,
    A,
    Img,
    Hr,
}

/// The elements `classes` can name, by tag name: the one list `from_tag`, `tag` and the
/// error message read.
const ELEMENTS: [(&str, Element); 17] = [
    ("p", Element::P),
    ("h1", Element::H(1)),
    ("h2", Element::H(2)),
    ("h3", Element::H(3)),
    ("h4", Element::H(4)),
    ("h5", Element::H(5)),
    ("h6", Element::H(6)),
    ("blockquote", Element::Blockquote),
    ("ul", Element::Ul),
    ("ol", Element::Ol),
    ("li", Element::Li),
    ("pre", Element::Pre),
    ("code", Element::Code),
    ("table", Element::Table),
    ("a", Element::A),
    ("img", Element::Img),
    ("hr", Element::Hr),
];

impl Element {
    /// The element for a tag name (`'h1'` to `'h6'`, `'ul'`, ...), or `None`.
    #[must_use]
    pub fn from_tag(tag: &str) -> Option<Self> {
        ELEMENTS.iter().find(|(name, _)| *name == tag).map(|(_, element)| *element)
    }

    /// The tag names `classes` accepts, comma-separated, for error messages.
    #[must_use]
    pub fn tags() -> String {
        ELEMENTS.iter().map(|(name, _)| *name).collect::<Vec<_>>().join(", ")
    }

    /// The start of this element's opening tag (`<p`, `<h2`, ...).
    fn open(self) -> &'static str {
        match self {
            Self::P => "<p",
            Self::H(1) => "<h1",
            Self::H(2) => "<h2",
            Self::H(3) => "<h3",
            Self::H(4) => "<h4",
            Self::H(5) => "<h5",
            Self::H(_) => "<h6",
            Self::Blockquote => "<blockquote",
            Self::Ul => "<ul",
            Self::Ol => "<ol",
            Self::Li => "<li",
            Self::Pre => "<pre",
            Self::Code => "<code",
            Self::Table => "<table",
            Self::A => "<a",
            Self::Img => "<img",
            Self::Hr => "<hr",
        }
    }

    fn of(value: &NodeValue) -> Option<Self> {
        Some(match value {
            NodeValue::Paragraph => Self::P,
            NodeValue::Heading(h) => Self::H(h.level),
            NodeValue::BlockQuote => Self::Blockquote,
            NodeValue::List(list) => match list.list_type {
                comrak::nodes::ListType::Bullet => Self::Ul,
                comrak::nodes::ListType::Ordered => Self::Ol,
            },
            NodeValue::Item(_) | NodeValue::TaskItem(_) => Self::Li,
            NodeValue::CodeBlock(_) => Self::Pre,
            NodeValue::Code(_) => Self::Code,
            NodeValue::Table(_) => Self::Table,
            NodeValue::Link(_) => Self::A,
            NodeValue::Image(_) => Self::Img,
            NodeValue::ThematicBreak => Self::Hr,
            _ => return None,
        })
    }
}

/// The GitHub extensions, each on by default.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[allow(clippy::struct_excessive_bools)]
pub struct Extensions {
    pub table: bool,
    pub strikethrough: bool,
    pub tasklist: bool,
    pub autolink: bool,
    pub footnotes: bool,
}

impl Default for Extensions {
    fn default() -> Self {
        Self { table: true, strikethrough: true, tasklist: true, autolink: true, footnotes: true }
    }
}

/// One `markdown:`-style prefix's settings: how its values convert.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Settings {
    pub raw_html: RawHtml,
    /// `false` renders an image as its alt text, so untrusted text can't make the page load a
    /// remote image.
    pub images: bool,
    /// Drop the `<p>` around output that is a single paragraph (titles, one-liners).
    pub inline: bool,
    /// GitHub-style heading ids, with this prefix (`Some("")` for none); `None`: no ids.
    pub heading_ids: Option<String>,
    /// `class` attributes per element. Values are attribute text, escaped when written.
    pub classes: Vec<(Element, String)>,
    pub extensions: Extensions,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            raw_html: RawHtml::Escape,
            images: true,
            inline: false,
            heading_ids: None,
            classes: Vec::new(),
            extensions: Extensions::default(),
        }
    }
}

/// [`Settings`] with the comrak options built from them, ready to convert.
#[derive(Debug)]
pub struct Profile {
    settings: Settings,
    options: Options<'static>,
    /// `classes`, with each value as ` class="..."` (escaped).
    class_attrs: Vec<(Element, String)>,
}

impl Profile {
    #[must_use]
    pub fn new(settings: Settings) -> Self {
        let mut options = Options::default();
        let ext = &mut options.extension;
        ext.table = settings.extensions.table;
        ext.strikethrough = settings.extensions.strikethrough;
        ext.tasklist = settings.extensions.tasklist;
        ext.autolink = settings.extensions.autolink;
        ext.footnotes = settings.extensions.footnotes;
        ext.header_id_prefix.clone_from(&settings.heading_ids);
        // Each heading's anchor link points at its id, prefix included.
        ext.header_id_prefix_in_href = true;
        match settings.raw_html {
            RawHtml::Escape => options.render.escape = true,
            RawHtml::Allow => options.render.r#unsafe = true,
        }
        let class_attrs = settings
            .classes
            .iter()
            .map(|(element, class)| {
                let mut attr = String::from(" class=\"");
                // Attribute text: the escaping `comrak::html::escape` does for `href` values.
                let _ = comrak::html::escape(&mut attr, class);
                attr.push('"');
                (*element, attr)
            })
            .collect();
        Self { settings, options, class_attrs }
    }

    #[must_use]
    pub fn settings(&self) -> &Settings {
        &self.settings
    }

    /// The HTML for `text`.
    #[must_use]
    pub fn to_html(&self, text: &str) -> String {
        let arena = Arena::new();
        let root = parse_document(&arena, text, &self.options);
        let pending = Rc::new(Cell::new(None));
        let mut out =
            ClassWriter { out: String::with_capacity(text.len() + text.len() / 4 + 16), pending: Rc::clone(&pending) };
        let state = State { profile: self, pending };
        let plugins = Plugins::default();
        // Writing to a `String` cannot fail, and the formatter only fails when the writer does.
        let _ = format_document_with_formatter(root, &self.options, &mut out, &plugins, formatter, state);
        out.out
    }
}

/// The formatter's per-render state.
struct State<'p> {
    profile: &'p Profile,
    /// The class attribute for the opening tag being written (shared with [`ClassWriter`]).
    pending: Rc<Cell<Option<Pending<'p>>>>,
}

/// A class attribute waiting for its element's opening tag.
#[derive(Clone, Copy)]
struct Pending<'p> {
    /// The tag start it belongs to (`<p`), and how many of its bytes were written so far.
    open: &'static str,
    matched: usize,
    /// The tag start was followed by a tag boundary (` `, `>`, `/`), so it isn't `<pre` for `<p`.
    bounded: bool,
    attr: &'p str,
}

/// comrak's default rendering, with three changes: `images: false` writes an image's alt text
/// instead, `inline` drops a lone paragraph's tags, and `classes` adds a class attribute to
/// the opening tag of the elements it names.
///
/// A class is armed when its node starts and only ever lands in that node's own opening tag:
/// the writer checks the tag ([`ClassWriter`]), and whatever is left unused is dropped when
/// the node's call returns (a paragraph in a tight list writes no tag). An image's tag ends
/// on its exit (` />` after the alt text), so its class lasts until then. Raw HTML and the
/// tags of other nodes never get one.
fn formatter<'a>(
    context: &mut Context<'_, '_, State<'_>>,
    node: &'a AstNode<'a>,
    entering: bool,
) -> Result<ChildRendering, fmt::Error> {
    let value = &node.data().value;
    let profile = context.user.profile;
    if !profile.settings.images && matches!(value, NodeValue::Image(_)) {
        // The alt text, escaped, as an image's children render in its `alt` attribute.
        return Ok(ChildRendering::Plain);
    }
    if profile.settings.inline && matches!(value, NodeValue::Paragraph) && is_lone_paragraph(node) {
        return Ok(ChildRendering::HTML);
    }
    if profile.class_attrs.is_empty() {
        return format_node_default(context, node, entering);
    }
    let image = matches!(value, NodeValue::Image(_));
    if entering
        && let Some(element) = Element::of(value)
        && let Some((_, attr)) = profile.class_attrs.iter().find(|(e, _)| *e == element)
    {
        context.user.pending.set(Some(Pending { open: element.open(), matched: 0, bounded: false, attr }));
    }
    let rendering = format_node_default(context, node, entering);
    if !(image && entering) {
        context.user.pending.set(None);
    }
    rendering
}

/// The document's only child is this paragraph.
fn is_lone_paragraph<'a>(node: &'a AstNode<'a>) -> bool {
    node.parent().is_some_and(|parent| {
        matches!(parent.data().value, NodeValue::Document)
            && node.previous_sibling().is_none()
            && node.next_sibling().is_none()
    })
}

/// The output, inserting a pending class attribute into its element's opening tag: before
/// the tag's first `>`, or before ` />` for a void element (`<hr />`, `<img ... />`). The tag
/// must be the next thing written (after whitespace) and start with the element's tag name;
/// anything else drops the class, so it can't land elsewhere. comrak writes a tag in pieces
/// (`<h` then `2`), so the match carries across writes.
struct ClassWriter<'p> {
    out: String,
    pending: Rc<Cell<Option<Pending<'p>>>>,
}

impl fmt::Write for ClassWriter<'_> {
    fn write_str(&mut self, mut s: &str) -> fmt::Result {
        let Some(mut p) = self.pending.take() else {
            self.out.push_str(s);
            return Ok(());
        };
        if p.matched == 0 {
            let rest = s.trim_start();
            self.out.push_str(&s[..s.len() - rest.len()]);
            s = rest;
        }
        if p.matched < p.open.len() && !s.is_empty() {
            let want = &p.open.as_bytes()[p.matched..];
            let n = want.len().min(s.len());
            if s.as_bytes()[..n] != want[..n] {
                // Not this element's tag: drop the class.
                self.out.push_str(s);
                return Ok(());
            }
            // `want` is ASCII, so `n` is a character boundary of `s`.
            self.out.push_str(&s[..n]);
            p.matched += n;
            s = &s[n..];
        }
        if p.matched == p.open.len() && !p.bounded && !s.is_empty() {
            if !s.starts_with([' ', '>', '/', '\n']) {
                self.out.push_str(s);
                return Ok(());
            }
            p.bounded = true;
        }
        let Some(at) = s.find('>').filter(|_| p.bounded) else {
            self.out.push_str(s);
            self.pending.set(Some(p));
            return Ok(());
        };
        self.out.push_str(&s[..at]);
        if self.out.ends_with(" /") {
            self.out.truncate(self.out.len() - 2);
            self.out.push_str(p.attr);
            self.out.push_str(" /");
        } else {
            self.out.push_str(p.attr);
        }
        self.out.push_str(&s[at..]);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn html(settings: Settings, text: &str) -> String {
        Profile::new(settings).to_html(text)
    }

    #[test]
    fn commonmark_and_gfm() {
        let s = Settings::default();
        assert_eq!(html(s.clone(), "# Hi *there*"), "<h1>Hi <em>there</em></h1>\n");
        assert_eq!(html(s.clone(), "~~gone~~"), "<p><del>gone</del></p>\n");
        assert_eq!(
            html(s.clone(), "see https://example.com"),
            "<p>see <a href=\"https://example.com\">https://example.com</a></p>\n"
        );
        assert_eq!(html(s, "```rust\nfn f() {}\n```"), "<pre><code class=\"language-rust\">fn f() {}\n</code></pre>\n");
    }

    #[test]
    fn raw_html_escaped_by_default() {
        assert_eq!(html(Settings::default(), "<b>x</b>"), "<p>&lt;b&gt;x&lt;/b&gt;</p>\n");
        let allow = Settings { raw_html: RawHtml::Allow, ..Settings::default() };
        assert_eq!(html(allow, "<b>x</b>"), "<p><b>x</b></p>\n");
    }

    #[test]
    fn images_off_renders_alt_text() {
        let s = Settings { images: false, ..Settings::default() };
        assert_eq!(html(s, "a ![the *logo*](http://x/y.png \"t\") b"), "<p>a the logo b</p>\n");
    }

    #[test]
    fn inline_drops_a_lone_paragraph() {
        let s = Settings { inline: true, ..Settings::default() };
        assert_eq!(html(s.clone(), "Hello *world*"), "Hello <em>world</em>");
        assert_eq!(html(s, "one\n\ntwo"), "<p>one</p>\n<p>two</p>\n");
    }

    #[test]
    fn classes_on_markdown_elements_only() {
        let s = Settings {
            raw_html: RawHtml::Allow,
            classes: vec![
                (Element::Table, "table is-striped".into()),
                (Element::Pre, "code".into()),
                (Element::Hr, "rule".into()),
                (Element::H(2), "t\"x".into()),
                (Element::P, "p".into()),
            ],
            heading_ids: Some(String::new()),
            ..Settings::default()
        };
        let out = html(s, "## Title\n\n---\n\n```\nx\n```\n\n| a |\n|---|\n| 1 |\n\n<p>raw</p>\n");
        assert!(out.contains("<h2 id=\"title\" class=\"t&quot;x\">"), "{out}");
        assert!(out.contains("<hr class=\"rule\" />"), "{out}");
        assert!(out.contains("<pre class=\"code\"><code>"), "{out}");
        assert!(out.contains("<table class=\"table is-striped\">"), "{out}");
        assert!(out.contains("<p>raw</p>"), "{out}");
    }
}
