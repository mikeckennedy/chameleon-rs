// Ported from Chameleon's src/chameleon/compiler.py (`Interpolator.__call__` with
// `braces_optional_regex` / `braces_required_regex`, as `StringExpr` and `ProxyExpr` use it:
// no translation, no entity decoding), modified: rewritten in Rust over byte offsets of a
// `Tok`, with the regex searched by hand.

//! `string:` interpolation: `${expression}` and, unless braces are required, `$name`.
//!
//! Chameleon's quirks are kept: the longest candidate is tried first and, when it does not
//! compile, the search is repeated on the match minus its last character, which can find a
//! later `$name`; the text then advances by the length of the match that compiled, from where
//! the first match started (`'${1 + $b}'` with `b='B'` is `'B1 + B}'`).

use pyo3::prelude::*;
use pyo3::types::PyString;

use chameleon_core::{ExprCompileError, Tok};

use crate::compiler::CompileResult;
use crate::expr::{Expr, StrPart};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Match {
    start: usize,
    end: usize,
    /// The `expression` or `variable` group (byte range); empty for `${}`.
    group: (usize, usize),
}

/// `\$({(?P<expression>.*)}|(?P<variable>[A-Za-z][A-Za-z0-9_]*))` (DOTALL), or only the
/// braces form when `braces_required`: the leftmost `$` where either alternative matches. The
/// greedy `.*` runs to the last `}` of the string.
fn search(s: &str, braces_required: bool) -> Option<Match> {
    let bytes = s.as_bytes();
    let last_close = s.rfind('}');
    for (p, _) in s.match_indices('$') {
        match bytes.get(p + 1) {
            Some(b'{') => {
                if let Some(close) = last_close
                    && close >= p + 2
                {
                    return Some(Match { start: p, end: close + 1, group: (p + 2, close) });
                }
            }
            Some(b) if !braces_required && b.is_ascii_alphabetic() => {
                let len = bytes[p + 1..].iter().take_while(|c| c.is_ascii_alphanumeric() || **c == b'_').count();
                return Some(Match { start: p, end: p + 1 + len, group: (p + 1, p + 1 + len) });
            }
            _ => {}
        }
    }
    None
}

/// Splits `expression` into text and compiled sub-expressions. `compile(source, candidate)`
/// compiles one sub-expression (`candidate`: a cheap structural rejection is fine, because
/// the last failure is compiled again without it for the parser's own message).
pub(crate) fn interpolate<F>(
    py: Python<'_>,
    expression: &Tok,
    braces_required: bool,
    surrogate_base: Option<u32>,
    mut compile: F,
) -> CompileResult<Vec<StrPart>>
where
    F: FnMut(&Tok, bool) -> CompileResult<Expr>,
{
    let mut parts = Vec::new();
    // Static text, its lone surrogates (mapped to the private-use block) mapped back.
    let text_part = |parts: &mut Vec<StrPart>, s: &str| -> CompileResult<()> {
        let s = crate::convert::source_str(py, s, surrogate_base).map_err(ExprCompileError::Other)?;
        parts.push(StrPart::Text(s.unbind()));
        Ok(())
    };
    let mut text = expression.clone();
    while !text.is_empty() {
        let mut matched = text.clone();
        let Some(mut m) = search(&matched.text, braces_required) else {
            text_part(&mut parts, &text.text.replace("$$", "$"))?;
            break;
        };
        let part = text.text.get(..m.start).unwrap_or_default().to_owned();
        text = text.slice_from(m.start);
        if !part.is_empty() {
            let dollars = part.bytes().rev().take_while(|&b| b == b'$').count();
            text_part(&mut parts, &part.replace("$$", "$"))?;
            if dollars % 2 == 1 {
                // An odd run of `$` escapes this one: drop it and carry on after it.
                text = text.slice_from(1);
                continue;
            }
        }
        loop {
            let string = matched.slice(m.group.0, m.group.1);
            if string.is_empty() {
                // `${}`: the match itself, as text.
                text_part(&mut parts, matched.text.get(m.start..m.end).unwrap_or_default())?;
                break;
            }
            match compile(&string, true) {
                Ok(expr) => {
                    parts.push(StrPart::Expr(expr));
                    break;
                }
                Err(ExprCompileError::Invalid { message, token }) => {
                    matched = matched.slice(m.start, m.end - 1);
                    if let Some(next) = search(&matched.text, braces_required) {
                        m = next;
                        continue;
                    }
                    // The last candidate failed: compile it once more without the structural
                    // shortcut, for the parser's own message.
                    return Err(match compile(&string, false) {
                        Err(e) => e,
                        Ok(_) => ExprCompileError::Invalid { message, token },
                    });
                }
                Err(e) => return Err(e),
            }
        }
        text = text.slice_from(m.end - m.start);
    }
    Ok(merge(py, parts))
}

/// Adjacent text parts joined (the result is the same: a single text node and a joined
/// string are equal `str`s).
fn merge(py: Python<'_>, parts: Vec<StrPart>) -> Vec<StrPart> {
    let mut merged: Vec<StrPart> = Vec::with_capacity(parts.len());
    for part in parts {
        if let (Some(StrPart::Text(prev)), StrPart::Text(s)) = (merged.last_mut(), &part)
            // Joined by Python, so lone surrogates survive.
            && let Ok(joined) = prev.bind(py).add(s.bind(py)).and_then(|j| Ok(j.cast_into::<PyString>()?))
        {
            *prev = joined.unbind();
            continue;
        }
        merged.push(part);
    }
    merged
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn search_follows_the_regex() {
        let m = |s: &'static str, r: bool| search(s, r).map(|m| (m.start, m.end, &s[m.group.0..m.group.1]));
        assert_eq!(m("a ${x} b ${y}", true), Some((2, 13, "x} b ${y")));
        assert_eq!(m("a $x.y", false), Some((2, 4, "x")));
        assert_eq!(m("a $x.y", true), None);
        assert_eq!(m("$1 ${", false), None);
        assert_eq!(m("$$x", false), Some((1, 3, "x")));
        assert_eq!(m("${}", false), Some((0, 3, "")));
        assert_eq!(m("${ $b", false), Some((3, 5, "b")));
    }
}
