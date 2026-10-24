//! Markdown to HTML for the `markdown:` expression type (Phase 11b): `CommonMark` plus the GitHub
//! extensions, through comrak. A [`Profile`] is built once, when the application configures
//! it; [`Profile::to_html`] converts one value at render time and reads nothing else.

use std::cell::Cell;
use std::fmt::{self, Write as _};
use std::rc::Rc;

use comrak::html::{ChildRendering, Context, format_document_with_formatter, format_node_default, render_sourcepos};
use comrak::nodes::{AstNode, NodeValue, TableAlignment};
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
    /// `options` with tables off, for text whose tables would be too large ([`table_cells`]);
    /// `None` when tables are off already.
    no_tables: Option<Options<'static>>,
    /// `classes`, with each value as ` class="..."` (escaped).
    class_attrs: Vec<(Element, String)>,
}

/// The table cells one conversion may create: this many, or one per byte of text if that is
/// more. comrak gives every row of a table as many cells as its header, so a short text can
/// ask for millions (four sparse 700-column tables in 22 KB take about 500 MB); its own limit
/// is per table.
const TABLE_CELLS_MIN: usize = 100_000;

/// At least the number of table cells comrak would create for `text`: per run of lines between
/// blank ones (a table ends at a blank line), the widest delimiter row there times the run's
/// lines. A delimiter row (`|:--|--:|`) has one run of `-` per cell; the `-` runs at the end
/// of any line, among only `|`, `-`, `:` and spaces, count as one, wherever it is (in a quote,
/// a list, a code block, after text).
/// Lines end at `\n`, `\r\n` or `\r`, as comrak's do.
fn table_cells(text: &str) -> usize {
    let (mut total, mut columns, mut lines) = (0usize, 0usize, 0usize);
    let mut rest = text;
    while !rest.is_empty() {
        let end = rest.find(['\n', '\r']).unwrap_or(rest.len());
        let line = &rest[..end];
        let next = if rest[end..].starts_with("\r\n") { end + 2 } else { (end + 1).min(rest.len()) };
        rest = &rest[next..];
        if line.bytes().all(|b| b == b' ' || b == b'\t') {
            total = total.saturating_add(columns.saturating_mul(lines));
            (columns, lines) = (0, 0);
            continue;
        }
        lines += 1;
        // A delimiter row ends its line, after whatever container markers start it (`> `, `- `).
        let bytes = line.as_bytes();
        let from = bytes.iter().rposition(|b| !matches!(b, b'|' | b'-' | b':' | b' ' | b'\t' | 0x0b | 0x0c));
        let row = &bytes[from.map_or(0, |i| i + 1)..];
        let cells = row.iter().zip(row.iter().skip(1)).filter(|&(&a, &b)| a != b'-' && b == b'-').count()
            + usize::from(row.first() == Some(&b'-'));
        columns = columns.max(cells);
    }
    total.saturating_add(columns.saturating_mul(lines))
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
        let no_tables = options.extension.table.then(|| {
            let mut no_tables = options.clone();
            no_tables.extension.table = false;
            no_tables
        });
        Self { settings, options, no_tables, class_attrs }
    }

    #[must_use]
    pub fn settings(&self) -> &Settings {
        &self.settings
    }

    /// The HTML for `text`. Text whose tables would take more than [`TABLE_CELLS_MIN`] cells
    /// (or one per byte) converts with tables off: their rows show as paragraph text, as rows
    /// past comrak's own per-table limit do.
    #[must_use]
    pub fn to_html(&self, text: &str) -> String {
        let options = match &self.no_tables {
            Some(no_tables) if table_cells(text) > TABLE_CELLS_MIN.max(text.len()) => no_tables,
            _ => &self.options,
        };
        let arena = Arena::new();
        let root = parse_document(&arena, text, options);
        let pending = Rc::new(Cell::new(None));
        let mut out =
            ClassWriter { out: String::with_capacity(text.len() + text.len() / 4 + 16), pending: Rc::clone(&pending) };
        let state = State { profile: self, pending, column: 0 };
        let plugins = Plugins::default();
        // Writing to a `String` cannot fail, and the formatter only fails when the writer does.
        let _ = format_document_with_formatter(root, options, &mut out, &plugins, formatter, state);
        out.out
    }
}

/// The formatter's per-render state.
struct State<'p> {
    profile: &'p Profile,
    /// The class attribute for the opening tag being written (shared with [`ClassWriter`]).
    pending: Rc<Cell<Option<Pending<'p>>>>,
    /// The next table cell's column in the row being written ([`table_cell`]).
    column: usize,
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

/// comrak's default rendering, with four changes: `images: false` writes an image's alt text
/// instead, `inline` drops a lone paragraph's tags, `classes` adds a class attribute to
/// the opening tag of the elements it names, and table cells render in [`table_cell`].
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
    match value {
        NodeValue::TableRow(_) if entering => context.user.column = 0,
        NodeValue::TableCell => return table_cell(context, node, entering),
        _ => {}
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

/// A table cell, written as comrak's `render_table_cell` writes it. comrak finds the cell's
/// column by walking the row from its first cell, for every cell: quadratic in the columns,
/// so a 64 KB table of 16,000 columns took 10 s (a denial of service a security review found).
/// Here the column is counted as the row's cells render, in order.
fn table_cell<'a>(
    context: &mut Context<'_, '_, State<'_>>,
    node: &'a AstNode<'a>,
    entering: bool,
) -> Result<ChildRendering, fmt::Error> {
    let row = node.parent();
    let header = row.is_some_and(|row| matches!(row.data().value, NodeValue::TableRow(true)));
    if !entering {
        context.write_str(if header { "</th>" } else { "</td>" })?;
        return Ok(ChildRendering::HTML);
    }
    let column = context.user.column;
    context.user.column += 1;
    let alignment = row.and_then(comrak::arena_tree::Node::parent).and_then(|table| match &table.data().value {
        NodeValue::Table(table) => table.alignments.get(column).copied(),
        _ => None,
    });
    context.cr()?;
    context.write_str(if header { "<th" } else { "<td" })?;
    render_sourcepos(context, node)?;
    context.write_str(match alignment {
        Some(TableAlignment::Left) => " align=\"left\">",
        Some(TableAlignment::Right) => " align=\"right\">",
        Some(TableAlignment::Center) => " align=\"center\">",
        Some(TableAlignment::None) | None => ">",
    })?;
    Ok(ChildRendering::HTML)
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

    #[test]
    fn table_cells_render_as_comrak_does() {
        let tables = [
            "| a | b |\n|---|---|\n| 1 | 2 |",
            "| l | c | r | n |\n|:--|:-:|--:|---|\n| 1 | 2 | 3 | 4 |\n| *x* | `y` | <b> | z |",
            "| a | b | c |\n|:-:|---|--:|\n| short |\n| 1 | 2 | 3 | 4 | 5 |",
            "| h |\n|---|",
            "> | q | t |\n> |---|:-:|\n> | 1 | 2 |\n\n- | in | list |\n  |----|-----:|\n  | 1 | 2 |",
        ];
        for settings in
            [Settings::default(), Settings { classes: vec![(Element::Table, "t".into())], ..Settings::default() }]
        {
            let profile = Profile::new(settings);
            for table in tables {
                let expected = comrak::markdown_to_html(table, &profile.options);
                let expected = if profile.class_attrs.is_empty() {
                    expected
                } else {
                    expected.replace("<table>", "<table class=\"t\">")
                };
                assert_eq!(profile.to_html(table), expected, "{table}");
            }
        }
    }

    #[test]
    fn wide_tables_render_in_linear_time() {
        // A security review's DoS: 16,000 columns took 10 s in comrak's own cell rendering.
        let columns = 40_000;
        // Full rows: one-cell rows would make comrak fill in 320,000 cells, which `table_cells`
        // refuses for this length of text.
        let text = format!(
            "{}\n{}\n{}",
            "|a".repeat(columns),
            "|-".repeat(columns),
            format!("{}\n", "|b".repeat(columns)).repeat(8)
        );
        let start = std::time::Instant::now();
        let out = html(Settings::default(), &text);
        assert_eq!(out.matches("<td>").count(), 8 * columns);
        assert!(start.elapsed() < std::time::Duration::from_secs(10), "{:?}", start.elapsed());
    }

    /// The cells comrak creates for `text`.
    fn comrak_cells(text: &str, options: &Options<'_>) -> usize {
        let arena = Arena::new();
        let root = parse_document(&arena, text, options);
        root.descendants().filter(|node| matches!(node.data().value, NodeValue::TableCell)).count()
    }

    #[test]
    fn sparse_tables_past_the_budget_convert_without_tables() {
        // A security review's memory DoS: four sparse tables of 700 columns and 700 one-cell rows,
        // 22 KB of text, made comrak create two million cells (about 500 MB). Each table is
        // under comrak's own per-table limit.
        let one = format!("|{}\n|{}\n{}", "a|".repeat(700), "-|".repeat(700), "|x|\n".repeat(700));
        let text = [one.as_str(); 4].join("\n\n");
        let profile = Profile::new(Settings::default());
        assert!(comrak_cells(&text, &profile.options) > 1_900_000);
        assert!(table_cells(&text) > TABLE_CELLS_MIN.max(text.len()));
        let out = profile.to_html(&text);
        assert!(!out.contains("<table") && out.len() < 4 * text.len(), "{}", out.len());
        // One such table is within the budget, and still a table.
        let small = format!("|{}\n|{}\n{}", "a|".repeat(100), "-|".repeat(100), "|x|\n".repeat(100));
        assert!(profile.to_html(&small).contains("<table>"));
    }

    #[test]
    fn table_cells_counts_line_endings_as_comrak_does() {
        let profile = Profile::new(Settings::default());
        for text in ["|a|b|\r|-|-|\r|x|\r|y|", "|a|b|\r\n|-|-|\r\n|x|\r\n\r\n|y|", "> |a|b|\n> |-|-|\n> |x|\n>\n> |y|"]
        {
            let cells = comrak_cells(text, &profile.options);
            assert!(cells > 0 && table_cells(text) >= cells, "{text:?}: {} < {cells}", table_cells(text));
        }
    }

    proptest::proptest! {
        #![proptest_config(proptest::prelude::ProptestConfig::with_cases(2000))]
        /// `table_cells` never counts fewer cells than comrak creates, whatever the text's
        /// containers, line endings and indentation.
        #[test]
        fn table_cells_is_an_upper_bound(text in "([|:x> \t-]{0,12}(\n|\r\n|\r)){0,14}") {
            let profile = Profile::new(Settings::default());
            let cells = comrak_cells(&text, &profile.options);
            proptest::prop_assert!(table_cells(&text) >= cells, "{:?}: {} < {}", text, table_cells(&text), cells);
        }

        #[test]
        fn table_cells_bounds_list_and_quote_tables(
            text in "((- |  |> |1\\. |    )?(\\|[ax]?){1,5}(\\|[:-]{1,3}){0,4}\\|?(\n|\r\n| \n)){0,12}"
        ) {
            let profile = Profile::new(Settings::default());
            let cells = comrak_cells(&text, &profile.options);
            proptest::prop_assert!(table_cells(&text) >= cells, "{:?}: {} < {}", text, table_cells(&text), cells);
        }
    }
}
