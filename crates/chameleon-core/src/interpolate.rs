// Ported from Chameleon's src/chameleon/compiler.py (`Interpolator.__call__`), modified:
// rewritten in Rust over byte offsets, with the `\$({(?P<expression>.*)})` regex (DOTALL,
// greedy) searched by hand.

//! Splitting text with `${...}` into static parts and expressions.
//!
//! Where an expression ends is decided by the expression parser: for each `${`, the longest
//! candidate (up to the last `}`) is tried first, then the next shorter one, and so on.

use crate::entities::decode_htmlentities;
use crate::error::{CompileError, ErrorKind};
use crate::expr::{ExprCompileError, ExprCompiler, ExprContext};
use crate::nodes::Part;
use crate::source::Span;

/// The regex search: the first `${` that has a `}` after it, and the last such `}`.
/// Returns `(start, close)` as offsets into `s`, `close` being the `}`.
fn search(s: &str) -> Option<(usize, usize)> {
    let start = s.find("${")?;
    let close = s.rfind('}')?;
    (close > start + 1).then_some((start, close))
}

/// Interpolates `span` of `src`. Text parts get Chameleon's `$$` -> `$` unescaping.
pub(crate) fn interpolate<C: ExprCompiler>(
    src: &str,
    span: Span,
    cx: ExprContext,
    compiler: &mut C,
) -> Result<Vec<Part>, CompileError<C::Error>> {
    let mut parts = Vec::new();
    let end = span.end as usize;
    let mut t = span.start as usize;
    while t < end {
        let text = src.get(t..end).unwrap_or_default();
        let Some((m_start, close)) = search(text) else {
            parts.push(Part::Text(text.replace("$$", "$")));
            break;
        };
        let start = t + m_start;
        let part = src.get(t..start).unwrap_or_default();
        t = start;
        if !part.is_empty() {
            let dollars = part.bytes().rev().take_while(|&b| b == b'$').count();
            parts.push(Part::Text(part.replace("$$", "$")));
            if dollars % 2 == 1 {
                // An odd run of `$` escapes this `${`: drop its `$` and carry on after it.
                t += 1;
                continue;
            }
        }
        let mut close_abs = t + close - m_start;
        loop {
            let expr_span = Span::new(start + 2, close_abs);
            let raw = expr_span.text(src);
            if raw.is_empty() {
                parts.push(Part::Text("${}".to_owned()));
                break;
            }
            let decoded = decode_htmlentities(raw)
                .map_err(|e| CompileError::new(ErrorKind::Expression, e.to_string(), expr_span))?;
            match compiler.compile(&decoded, expr_span, ExprContext { candidate: true, ..cx }) {
                Ok(id) => {
                    parts.push(Part::Expr(id));
                    break;
                }
                Err(ExprCompileError::Invalid { message }) => {
                    // Retry with the candidate up to the previous `}`.
                    let shorter = src.get(start..close_abs).unwrap_or_default();
                    if let Some((_, c)) = search(shorter) {
                        close_abs = start + c;
                    } else {
                        // The last candidate failed: compile it once more without the
                        // structural shortcut, for the parser's own message.
                        let exact = ExprContext { candidate: false, ..cx };
                        let message = match compiler.compile(&decoded, expr_span, exact) {
                            Err(ExprCompileError::Invalid { message }) => message,
                            Err(ExprCompileError::Other(e)) => return Err(CompileError::Expr(e)),
                            Ok(_) => message,
                        };
                        return Err(CompileError::new(ErrorKind::Expression, message, expr_span));
                    }
                }
                Err(ExprCompileError::Other(e)) => return Err(CompileError::Expr(e)),
            }
        }
        t = close_abs + 1;
    }
    Ok(merge_text(parts))
}

fn merge_text(parts: Vec<Part>) -> Vec<Part> {
    let mut merged: Vec<Part> = Vec::with_capacity(parts.len());
    for part in parts {
        match (merged.last_mut(), part) {
            (Some(Part::Text(prev)), Part::Text(s)) => prev.push_str(&s),
            (_, part) => merged.push(part),
        }
    }
    merged.retain(|p| !matches!(p, Part::Text(s) if s.is_empty()));
    merged
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::expr::ExprId;

    /// Accepts candidates with balanced braces and no `}` left over; records what it saw.
    #[derive(Default)]
    struct Toy {
        seen: Vec<String>,
    }

    impl ExprCompiler for Toy {
        type Error = ();
        fn compile(&mut self, source: &str, _: Span, _: ExprContext) -> Result<ExprId, ExprCompileError<()>> {
            self.seen.push(source.to_owned());
            let mut depth = 0i32;
            let mut in_str = false;
            for c in source.chars() {
                match c {
                    '"' => in_str = !in_str,
                    '{' if !in_str => depth += 1,
                    '}' if !in_str => depth -= 1,
                    _ => {}
                }
                if depth < 0 {
                    return Err(ExprCompileError::Invalid { message: "unbalanced".into() });
                }
            }
            if depth != 0 || in_str || source.trim().is_empty() {
                return Err(ExprCompileError::Invalid { message: "invalid".into() });
            }
            #[allow(clippy::cast_possible_truncation)]
            Ok(ExprId(self.seen.len() as u32 - 1))
        }
    }

    fn run(text: &str) -> (Vec<Part>, Vec<String>) {
        let mut toy = Toy::default();
        let parts = interpolate(text, Span::new(0, text.len()), ExprContext::default(), &mut toy).unwrap();
        (parts, toy.seen)
    }

    #[test]
    fn longest_candidate_first() {
        let (parts, seen) = run("a ${x} b ${y} c");
        assert_eq!(seen, ["x} b ${y", "x", "y"]);
        assert_eq!(
            parts,
            [
                Part::Text("a ".into()),
                Part::Expr(ExprId(1)),
                Part::Text(" b ".into()),
                Part::Expr(ExprId(2)),
                Part::Text(" c".into())
            ]
        );
    }

    #[test]
    fn braces_inside_expressions() {
        let (parts, _) = run("${\"}\"} ${ {\"a\": 1} }");
        assert!(matches!(parts.as_slice(), [Part::Expr(_), Part::Text(s), Part::Expr(_)] if s == " "));
    }

    #[test]
    fn dollar_escapes() {
        assert_eq!(run("$${x} $$$${x} $x ${").0, [Part::Text("${x} $${x} $x ${".into())]);
        assert_eq!(run("${}").0, [Part::Text("${}".into())]);
    }

    #[test]
    fn invalid_without_shorter_candidate_is_an_error() {
        let mut toy = Toy::default();
        let err = interpolate("${ }", Span::new(0, 4), ExprContext::default(), &mut toy).unwrap_err();
        assert!(matches!(err, CompileError::Template { kind: ErrorKind::Expression, .. }));
    }

    #[test]
    fn entities_are_decoded_before_parsing() {
        let (_, seen) = run("${a &amp;&amp; b}");
        assert_eq!(seen, ["a && b"]);
    }
}
