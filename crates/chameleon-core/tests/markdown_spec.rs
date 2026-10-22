//! The `CommonMark` 0.31.2 spec examples and cmark-gfm's extension examples, through the
//! `markdown:` converter (vendored in `tests/markdown_spec/`, see its README).
#![cfg(feature = "markdown")]

use chameleon_core::markdown::{Extensions, Profile, RawHtml, Settings};

struct Example {
    number: usize,
    section: String,
    markdown: String,
    html: String,
}

fn examples(spec: &str) -> Vec<Example> {
    let fence = "````````````````````````````````";
    let mut out = Vec::new();
    let mut section = String::new();
    let mut lines = spec.lines();
    while let Some(line) = lines.next() {
        if let Some(title) = line.strip_prefix("## ").or_else(|| line.strip_prefix("# ")) {
            title.trim().clone_into(&mut section);
        }
        let Some(rest) = line.strip_prefix(fence) else { continue };
        if !rest.trim().starts_with("example") {
            continue;
        }
        let (mut markdown, mut html, mut in_html) = (String::new(), String::new(), false);
        for line in lines.by_ref() {
            if line == fence {
                break;
            }
            if line == "." && !in_html {
                in_html = true;
                continue;
            }
            let target = if in_html { &mut html } else { &mut markdown };
            target.push_str(&line.replace('→', "\t"));
            target.push('\n');
        }
        out.push(Example { number: out.len() + 1, section: section.clone(), markdown, html });
    }
    out
}

/// Runs the examples (except those in a `skip` section) and returns the numbers of those that
/// differ. An expected output of `<IGNORE>` only checks that converting doesn't panic.
fn run(spec: &str, settings: Settings, skip: &[&str]) -> (usize, Vec<usize>) {
    let profile = Profile::new(settings);
    let mut failed = Vec::new();
    let mut ran = 0;
    for ex in examples(spec) {
        if skip.contains(&ex.section.as_str()) {
            continue;
        }
        ran += 1;
        let got = profile.to_html(&ex.markdown);
        if ex.html.trim() == "<IGNORE>" {
            continue;
        }
        if got != ex.html {
            eprintln!(
                "example {} ({}) differs:\n--- markdown\n{}--- expected\n{}--- got\n{}",
                ex.number, ex.section, ex.markdown, ex.html, got
            );
            failed.push(ex.number);
        }
    }
    (ran, failed)
}

#[test]
fn commonmark_spec() {
    let spec = include_str!("markdown_spec/commonmark-0.31.2.txt");
    let none = Extensions { table: false, strikethrough: false, tasklist: false, autolink: false, footnotes: false };
    let settings = Settings { raw_html: RawHtml::Allow, extensions: none, ..Settings::default() };
    let (ran, failed) = run(spec, settings, &[]);
    assert_eq!(ran, 652);
    assert!(failed.is_empty(), "{} of {ran} CommonMark examples differ: {failed:?}", failed.len());
}

#[test]
fn gfm_extensions_spec() {
    let spec = include_str!("markdown_spec/gfm-extensions.txt");
    let settings = Settings { raw_html: RawHtml::Allow, ..Settings::default() };
    // GitHub's tag filter (a few raw HTML tags escaped) is not one of the extensions: raw HTML
    // is escaped by default, and `raw_html='allow'` is for trusted content.
    let (ran, failed) = run(spec, settings, &["HTML tag filter"]);
    assert_eq!(ran, 29);
    // Strict, so a comrak upgrade that changes it gets noticed. Example 19, one line of many:
    // comrak doesn't autolink `http://🍄.ga/` (a domain starting with an emoji); cmark-gfm does.
    assert_eq!(failed, [19], "GFM examples differ: {failed:?}");
}

/// `classes` on every element, over every spec example (raw HTML allowed): taking the classes
/// out gives exactly the output without them, and each one sits directly in an opening tag of
/// its own element, never in raw HTML, a closing tag or another element's tag.
#[test]
fn classes_land_only_in_their_own_tags() {
    use chameleon_core::markdown::Element;

    let tags = [
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "ul",
        "ol",
        "li",
        "pre",
        "code",
        "table",
        "a",
        "img",
        "hr",
    ];
    let classes: Vec<(Element, String)> =
        tags.iter().map(|t| (Element::from_tag(t).expect("a known tag"), format!("__{t}__"))).collect();
    let specs = [include_str!("markdown_spec/commonmark-0.31.2.txt"), include_str!("markdown_spec/gfm-extensions.txt")];
    let mut checked = 0;
    for inline in [false, true] {
        for images in [true, false] {
            let base = Settings { raw_html: RawHtml::Allow, inline, images, ..Settings::default() };
            let plain = Profile::new(base.clone());
            let with_classes = Profile::new(Settings { classes: classes.clone(), ..base });
            for ex in specs.iter().flat_map(|spec| examples(spec)) {
                let out = with_classes.to_html(&ex.markdown);
                let mut stripped = out.clone();
                for t in tags {
                    stripped = stripped.replace(&format!(" class=\"__{t}__\""), "");
                }
                assert_eq!(stripped, plain.to_html(&ex.markdown), "example {} ({})", ex.number, ex.section);
                for t in tags {
                    let attr = format!(" class=\"__{t}__\"");
                    for (at, _) in out.match_indices(&attr) {
                        let open = out[..at].rfind('<').expect("inside a tag");
                        let tag = &out[open..at];
                        let name = tag[1..].split([' ', '/', '\n']).next().unwrap_or("");
                        assert!(
                            name == t && !tag.contains('>'),
                            "example {} ({}): {attr} in {tag:?}\n{out}",
                            ex.number,
                            ex.section
                        );
                    }
                }
                checked += 1;
            }
        }
    }
    assert_eq!(checked, 4 * (652 + 30));
}
