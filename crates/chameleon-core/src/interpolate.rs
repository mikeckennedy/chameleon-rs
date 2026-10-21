// Ported from Chameleon's src/chameleon/compiler.py (`Interpolator.__call__`), modified:
// rewritten in Rust over byte offsets, with the `\$({(?P<expression>.*)})` regex (DOTALL,
// greedy) searched by hand.

//! Splitting text with `${...}` into static parts and expressions.
//!
//! Where an expression ends is decided by the expression parser: for each `${`, the longest
//! candidate (up to the last `}`) is tried first, then the next shorter one, and so on.

use crate::entities::decode_htmlentities;
use crate::error::{CompileError, ErrorKind};
use crate::expr::{ExprCompileError, ExprCompiler, ExprContext, ExprId};
use crate::tok::Tok;

/// A piece of an interpolation: static text or an expression.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Part {
    Text(String),
    Expr(ExprId),
}

/// The regex search: the first `${` that has a `}` after it, and the last such `}`.
/// Returns `(start, close)` as offsets into `s`, `close` being the `}`.
fn search(s: &str) -> Option<(usize, usize)> {
    let start = s.find("${")?;
    let close = s.rfind('}')?;
    (close > start + 1).then_some((start, close))
}

/// Interpolates `text`. Text parts get Chameleon's `$$` -> `$` unescaping. Each expression is
/// passed with its position in `text` (Chameleon slices the `Token`).
pub(crate) fn interpolate<C: ExprCompiler>(
    text: &Tok,
    cx: ExprContext<'_>,
    compiler: &mut C,
) -> Result<Vec<Part>, CompileError<C::Error>> {
    let src = text.text.as_str();
    let mut parts = Vec::new();
    let end = src.len();
    let mut t = 0;
    // Chameleon's `body`: whether an earlier `${...}` produced code.
    let mut body = false;
    while t < end {
        let rest = &src[t..];
        let Some((m_start, close)) = search(rest) else {
            parts.push(Part::Text(rest.replace("$$", "$")));
            break;
        };
        let start = t + m_start;
        let part = &src[t..start];
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
        if body && text.pos.is_none() {
            // The target name of a later expression is `"%s_%d" % (name, text.pos)`, and a
            // plain `str` (a processing instruction's text) has no `pos`.
            return Err(CompileError::new(ErrorKind::Attribute, "'str' object has no attribute 'pos'", Tok::plain("")));
        }
        body = true;
        let mut close_abs = t + close - m_start;
        loop {
            let expr = text.slice(start + 2, close_abs);
            if expr.is_empty() {
                parts.push(Part::Text("${}".to_owned()));
                break;
            }
            let decoded = match decode_htmlentities(&expr.text) {
                Ok(d) => expr.with_text(d.into_owned()),
                Err(e) if e.surrogate => return Err(CompileError::unsupported(e.message, expr)),
                Err(e) => return Err(CompileError::new(ErrorKind::Value, e.message, Tok::plain(""))),
            };
            match compiler.compile(&decoded, ExprContext { candidate: true, ..cx }) {
                Ok(id) => {
                    parts.push(Part::Expr(id));
                    break;
                }
                Err(ExprCompileError::Invalid { message, token }) => {
                    // Retry with the candidate up to the previous `}`.
                    if let Some((_, c)) = search(&src[start..close_abs]) {
                        close_abs = start + c;
                        continue;
                    }
                    // The last candidate failed: compile it once more without the structural
                    // shortcut, for the parser's own message.
                    let exact = ExprContext { candidate: false, ..cx };
                    let (message, token) = match compiler.compile(&decoded, exact) {
                        Err(ExprCompileError::Invalid { message, token }) => (message, token),
                        Err(ExprCompileError::Other(e)) => return Err(CompileError::Expr(e)),
                        Ok(_) => (message, token),
                    };
                    return Err(CompileError::new(ErrorKind::Expression, message, token.unwrap_or(decoded)));
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
    use crate::tok::TokPos;

    /// Accepts candidates with balanced braces and no `}` left over; records what it saw.
    #[derive(Default)]
    pub(crate) struct Toy {
        pub(crate) seen: Vec<Tok>,
    }

    impl ExprCompiler for Toy {
        type Error = ();
        fn compile(&mut self, source: &Tok, _: ExprContext<'_>) -> Result<ExprId, ExprCompileError<()>> {
            self.seen.push(source.clone());
            let mut depth = 0i32;
            let mut in_str = false;
            for c in source.text.chars() {
                match c {
                    '"' => in_str = !in_str,
                    '{' if !in_str => depth += 1,
                    '}' if !in_str => depth -= 1,
                    _ => {}
                }
                if depth < 0 {
                    return Err(ExprCompileError::Invalid { message: "unbalanced".into(), token: None });
                }
            }
            if depth != 0 || in_str || source.text.trim().is_empty() {
                return Err(ExprCompileError::Invalid { message: "invalid".into(), token: None });
            }
            #[allow(clippy::cast_possible_truncation)]
            Ok(ExprId(self.seen.len() as u32 - 1))
        }

        fn compile_code(&mut self, _: &Tok) -> Result<ExprId, ExprCompileError<()>> {
            Ok(ExprId(0))
        }

        fn deferred(&mut self, _: &str, _: &Tok) -> Result<ExprId, ExprCompileError<()>> {
            Ok(ExprId(0))
        }
    }

    fn tok(text: &str) -> Tok {
        Tok { text: text.to_owned(), pos: Some(TokPos::at(0)) }
    }

    fn run(text: &str) -> (Vec<Part>, Vec<String>) {
        let mut toy = Toy::default();
        let parts = interpolate(&tok(text), ExprContext::default(), &mut toy).unwrap();
        (parts, toy.seen.into_iter().map(|t| t.text).collect())
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
    fn expressions_carry_their_positions() {
        let mut toy = Toy::default();
        interpolate(&tok("é ${x} ${y}"), ExprContext::default(), &mut toy).unwrap();
        let at: Vec<_> = toy.seen.iter().map(|t| (t.text.as_str(), t.pos.map(|p| p.chars))).collect();
        assert_eq!(at, [("x} ${y", Some(4)), ("x", Some(4)), ("y", Some(9))]);
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
        let err = interpolate(&tok("${ }"), ExprContext::default(), &mut toy).unwrap_err();
        assert!(matches!(err, CompileError::Template { kind: ErrorKind::Expression, .. }));
    }

    #[test]
    fn entities_are_decoded_before_parsing() {
        let (_, seen) = run("${a &amp;&amp; b}");
        assert_eq!(seen, ["a && b"]);
        let mut toy = Toy::default();
        let err = interpolate(&tok("${&#xzz;}"), ExprContext::default(), &mut toy).unwrap_err();
        assert_eq!(err.kind(), Some(ErrorKind::Value));
    }
}
