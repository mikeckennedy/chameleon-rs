// Ported from Chameleon's src/chameleon/i18n.py (`parse_attributes`, `WHITELIST`), modified:
// rewritten in Rust over `Tok`.

//! Parsing `i18n:` attribute values.

use crate::tok::Tok;

pub(crate) const I18N_WHITELIST: &[&str] = &[
    "translate",
    "domain",
    "context",
    "target",
    "source",
    "attributes",
    "data",
    "name",
    "mode",
    "xmlns",
    "xml",
    "comment",
    "ignore",
    "ignore-attributes",
];

/// Why `i18n:attributes` is invalid: the message and the token it points at
/// (Chameleon raises `CompilationError`).
pub(crate) type I18nError = (&'static str, Tok);

/// `parse_attributes` (`i18n:attributes`, XML mode): `(attribute, msgid)` in order.
pub(crate) fn parse_attributes(clause: &Tok) -> Result<Vec<(Tok, Option<Tok>)>, I18nError> {
    let mut d: Vec<(Tok, Option<Tok>)> = Vec::new();
    for spec in clause.split(";").into_iter().filter(|s| !s.is_empty()) {
        if spec.text.contains(',') {
            return Err(("Attribute must not contain comma. Use semicolon to list multiple attributes", spec));
        }
        let mut parts = spec.split_whitespace().into_iter();
        let (Some(attr), msgid, None) = (parts.next(), parts.next(), parts.next()) else {
            return Err(("Illegal i18n:attributes specification.", spec));
        };
        let attr = attr.strip();
        if d.iter().any(|(a, _)| a.text == attr.text) {
            return Err(("Attribute may only be specified once in i18n:attributes", attr));
        }
        d.push((attr, msgid));
    }
    Ok(d)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tok::TokPos;

    fn tok(s: &str) -> Tok {
        Tok { text: s.to_owned(), pos: Some(TokPos::at(0)) }
    }

    #[test]
    fn specs() {
        let d = parse_attributes(&tok("title msg-title; alt;")).unwrap();
        let got: Vec<_> = d.iter().map(|(a, m)| (a.as_str(), m.as_ref().map(Tok::as_str))).collect();
        assert_eq!(got, [("title", Some("msg-title")), (" alt", None)].map(|(a, m)| (a.trim(), m)));
        assert_eq!(parse_attributes(&tok("a b c")).unwrap_err().0, "Illegal i18n:attributes specification.");
        let (msg, at) = parse_attributes(&tok("a; b, c")).unwrap_err();
        assert!(msg.starts_with("Attribute must not contain comma"));
        // `Token.split(';')` does not count the separator: " b, c" is reported at 1, not 2.
        assert_eq!((at.as_str(), at.pos.map(|p| p.chars)), (" b, c", Some(1)));
        assert_eq!(parse_attributes(&tok("a; a")).unwrap_err().1.as_str(), "a");
        assert!(parse_attributes(&tok(" ;a")).is_err());
    }
}
