//! End-to-end core tests with a toy expression compiler and runtime. They cover control flow
//! (D2): text coalescing, attributes, content defaults, conditions, loop whitespace and
//! restore, frames and suspension. Value conversion is tested through Python (D13).

use std::collections::HashMap;
use std::sync::Arc;

use crate::{
    Call, Countdown, Done, Emitted, Escape, ExprCompileError, ExprCompiler, ExprContext, ExprId, NameId, Output,
    Program, Runtime, Span, Step, VmError, VmState, compile, escape_into, run,
};

#[derive(Clone, Debug, PartialEq)]
enum V {
    None,
    Int(i64),
    Str(String),
    List(Vec<V>),
    Default,
    /// A loop iterator: the items and an id (the position lives in the scope).
    Iter(Vec<V>, usize),
    /// A frame's code: index into the toy's programs.
    Code(usize),
    /// A frame's scope: index into the toy's scopes.
    Scope(usize),
}

/// Toy expressions: `none`, integers, `'strings'`, `[a, b]` lists of those, `default` (when
/// aliased), `call:N` (run program N as a frame), `wait:X` (suspend, resume with X's value),
/// and names.
#[derive(Clone, Debug)]
struct Expr {
    src: String,
    default_is_marker: bool,
    emitted: bool,
}

#[derive(Default)]
struct Toy {
    exprs: Vec<Expr>,
}

impl ExprCompiler for Toy {
    type Error = String;
    fn compile(&mut self, source: &str, _: Span, cx: ExprContext) -> Result<ExprId, ExprCompileError<String>> {
        let s = source.trim();
        if s.is_empty() || s.contains('}') || s.matches('\'').count() % 2 == 1 {
            return Err(ExprCompileError::Invalid { message: format!("invalid: {s:?}") });
        }
        if s == "boom" {
            return Err(ExprCompileError::Other("boom".into()));
        }
        self.exprs.push(Expr { src: s.to_owned(), default_is_marker: cx.default_is_marker, emitted: cx.emitted });
        Ok(ExprId(u32::try_from(self.exprs.len() - 1).unwrap()))
    }
}

struct World {
    programs: Vec<(Arc<Program>, Vec<Expr>)>,
    scopes: Vec<HashMap<String, V>>,
    code: usize,
    scope: usize,
    log: Vec<String>,
    iterators: usize,
}

impl World {
    fn names(&self) -> &[Box<str>] {
        self.programs[self.code].0.names()
    }

    fn name(&self, n: NameId) -> String {
        self.names()[n.0 as usize].to_string()
    }

    fn value(&mut self, s: &str, cx: &Expr) -> Result<Step<V>, String> {
        if let Some(rest) = s.strip_prefix("wait:") {
            return Ok(Step::Suspend(self.plain(rest, cx)?));
        }
        if let Some(rest) = s.strip_prefix("call:") {
            assert!(cx.emitted, "frame calls only where emitted");
            let code: usize = rest.parse().map_err(|_| "bad call")?;
            self.scopes.push(HashMap::from([("x".to_owned(), V::Str(format!("from {code}")))]));
            let scope = self.scopes.len() - 1;
            return Ok(Step::Call(Call {
                program: Arc::clone(&self.programs[code].0),
                code: V::Code(code),
                scope: V::Scope(scope),
            }));
        }
        Ok(Step::Ready(self.plain(s, cx)?))
    }

    fn plain(&self, s: &str, cx: &Expr) -> Result<V, String> {
        Ok(if s == "none" {
            V::None
        } else if s == "default" && cx.default_is_marker {
            V::Default
        } else if let Ok(n) = s.parse() {
            V::Int(n)
        } else if let Some(inner) = s.strip_prefix('\'').and_then(|r| r.strip_suffix('\'')) {
            V::Str(inner.to_owned())
        } else if let Some(inner) = s.strip_prefix('[').and_then(|r| r.strip_suffix(']')) {
            V::List(
                inner
                    .split(',')
                    .filter(|p| !p.trim().is_empty())
                    .map(|p| self.plain(p.trim(), cx))
                    .collect::<Result<_, _>>()?,
            )
        } else {
            self.scopes[self.scope].get(s).cloned().ok_or_else(|| format!("NameError: {s}"))?
        })
    }
}

impl Runtime for World {
    type Value = V;
    type Error = String;

    fn activate(&mut self, code: &V, scope: &V) -> Result<(), String> {
        let (V::Code(c), V::Scope(s)) = (code, scope) else { return Err("bad frame".into()) };
        self.code = *c;
        self.scope = *s;
        Ok(())
    }

    fn eval(&mut self, expr: ExprId, emitted: bool) -> Result<Step<V>, String> {
        let e = self.programs[self.code].1[expr.0 as usize].clone();
        assert_eq!(e.emitted, emitted, "{}", e.src);
        self.value(&e.src.clone(), &e)
    }

    fn emit(&mut self, value: V, esc: Escape, _lone: bool, out: &mut Output) -> Result<Emitted, String> {
        let s = match value {
            V::None | V::Default => return Ok(Emitted::Nothing),
            V::Int(n) => n.to_string(),
            V::Str(s) => s,
            other => format!("{other:?}"),
        };
        escape_into(s.as_bytes(), esc, out);
        Ok(Emitted::Value)
    }

    fn is_default(&mut self, value: &V) -> bool {
        *value == V::Default
    }

    fn truthy(&mut self, value: &V) -> Result<bool, String> {
        Ok(!matches!(value, V::None | V::Int(0) | V::List(_)) || matches!(value, V::List(l) if !l.is_empty()))
    }

    fn lookup(&mut self, name: NameId) -> Result<Option<V>, String> {
        let n = self.name(name);
        self.log.push(format!("backup {n}"));
        Ok(self.scopes[self.scope].get(&n).cloned())
    }

    fn set_null(&mut self, name: NameId) -> Result<(), String> {
        let n = self.name(name);
        self.log.push(format!("null {n}"));
        self.scopes[self.scope].insert(n, V::None);
        Ok(())
    }

    fn repeat(&mut self, names: &[NameId], iterable: V) -> Result<(V, Countdown<V>), String> {
        self.log.push(format!("repeat {}", self.name(names[0])));
        let V::List(items) = iterable else { return Err("not iterable".into()) };
        let len = items.len();
        self.iterators += 1;
        let len = i64::try_from(len).unwrap();
        // Loops over a name starting with `v` get a runtime-valued countdown.
        let index =
            if self.name(names[0]).starts_with('v') { Countdown::Value(V::Int(len)) } else { Countdown::Int(len) };
        Ok((V::Iter(items, self.iterators), index))
    }

    fn countdown(&mut self, index: &V) -> Result<(V, bool), String> {
        let V::Int(n) = index else { return Err("TypeError: unsupported operand".into()) };
        Ok((V::Int(n - 1), n - 1 > 0))
    }

    fn next(&mut self, iterator: &V) -> Result<Option<V>, String> {
        // The toy keeps the position in the scope under a hidden key, since values are cloned.
        let V::Iter(items, id) = iterator else { return Err("not an iterator".into()) };
        let key = format!("#it{id}");
        let pos = match self.scopes[self.scope].get(&key) {
            Some(V::Int(p)) => usize::try_from(*p).unwrap(),
            _ => 0,
        };
        let item = items.get(pos).cloned();
        if item.is_some() {
            self.scopes[self.scope].insert(key, V::Int(i64::try_from(pos + 1).unwrap()));
        } else {
            self.scopes[self.scope].remove(&key);
        }
        Ok(item)
    }

    fn assign(&mut self, names: &[NameId], item: V) -> Result<(), String> {
        let items = if names.len() == 1 {
            vec![item]
        } else {
            let V::List(items) = item else { return Err("cannot unpack".into()) };
            items
        };
        for (&n, v) in names.iter().zip(items) {
            let n = self.name(n);
            self.scopes[self.scope].insert(n, v);
        }
        Ok(())
    }

    fn restore(&mut self, name: NameId, backup: Option<V>) -> Result<(), String> {
        let n = self.name(name);
        self.log.push(format!("restore {n}"));
        match backup {
            Some(v) => self.scopes[self.scope].insert(n, v),
            None => self.scopes[self.scope].remove(&n),
        };
        Ok(())
    }
}

fn world(templates: &[&str], vars: &[(&str, V)]) -> World {
    let programs = templates
        .iter()
        .map(|t| {
            let mut toy = Toy::default();
            let p = compile(t, &mut toy).unwrap_or_else(|e| panic!("{t}: {e:?}"));
            (Arc::new(p), toy.exprs)
        })
        .collect();
    let scope = vars.iter().map(|(k, v)| ((*k).to_owned(), v.clone())).collect();
    World { programs, scopes: vec![scope], code: 0, scope: 0, log: Vec::new(), iterators: 0 }
}

fn render_world(w: &mut World) -> String {
    let mut st = VmState::new(Arc::clone(&w.programs[0].0), V::Code(0), V::Scope(0));
    loop {
        match run(&mut st, w).unwrap() {
            Done::Finished => break,
            Done::Suspended(v) => st.resume_with::<String>(v).unwrap(),
        }
    }
    assert_eq!(st.unwind_depth(), 0);
    assert_eq!(st.frame_depth(), 0);
    String::from_utf8(st.out.as_bytes().to_vec()).unwrap()
}

fn render(template: &str, vars: &[(&str, V)]) -> String {
    render_world(&mut world(&[template], vars))
}

#[test]
fn static_text_is_coalesced() {
    let mut toy = Toy::default();
    let p = compile("<div class=\"a\">\n  <p>hi</p><!-- c --></div>", &mut toy).unwrap();
    insta::assert_snapshot!(p.dump(), @r#"
      0 text "<div class=\"a\">\n  <p>hi</p><!-- c --></div>"
      1 end
    "#);
}

#[test]
fn listing_bytecode() {
    let mut toy = Toy::default();
    let src = "<ul>\n  <li tal:repeat=\"v vs\" class=\"x ${v}\"><a href=\"${v}\" tal:content=\"v\">d</a></li>\n</ul>";
    let p = compile(src, &mut toy).unwrap();
    insta::assert_snapshot!(p.dump(), @r#"
      0 text "<ul>\n  "
      1 loop ["v"] in e1 ws="\n  " next=10
      2 text "<li class=\"x "
      3 emit e0 Attr(Double)
      4 text "\"><a"
      5 attr e3 Attr(Double) " href=\"" "\""
      6 text ">"
      7 content e2 Content end=9
      8 text "d"
      9 text "</a></li>"
     10 loop_next body=2
     11 text "\n</ul>"
     12 end
    "#);
}

#[test]
fn interpolation_and_attributes() {
    let vars = [("x", V::Str("a&<b>".into())), ("n", V::None)];
    assert_eq!(
        render("<p a=\"${x}\" b=\"${n}\" c=\"[${n}]\">${x}${n}</p>", &vars),
        "<p a=\"a&amp;&lt;b&gt;\" c=\"[]\">a&amp;&lt;b&gt;</p>"
    );
    assert_eq!(render("<p a=\"${default}\">${'d'}</p>", &vars), "<p>d</p>");
}

#[test]
fn content_and_default() {
    assert_eq!(render("<p tal:content=\"default\">keep <b>me</b></p>", &[]), "<p>keep <b>me</b></p>");
    assert_eq!(render("<p tal:content=\"none\">gone</p>", &[]), "<p></p>");
    assert_eq!(render("<p tal:content=\"'x'\"/>", &[]), "<p>x</p>");
    assert_eq!(render("<p tal:content=\"structure '<b>'\">d</p>", &[]), "<p><b></p>");
}

#[test]
fn condition_wraps_repeat() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2)])), ("t", V::Int(1)), ("f", V::Int(0))];
    assert_eq!(render("<i tal:condition=\"f\">no</i><i tal:condition=\"t\">yes</i>", &vars), "<i>yes</i>");
    assert_eq!(render("<i tal:condition=\"xs\" tal:repeat=\"x xs\">${x}</i>", &vars), "<i>1</i>\n<i>2</i>");
}

#[test]
fn repeat_whitespace_and_restore_order() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2), V::Int(3)])), ("x", V::Str("outer".into()))];
    let mut w = world(&["<ul>\n    <li tal:repeat=\"x xs\">${x}</li>\n</ul>${x}"], &vars);
    assert_eq!(render_world(&mut w), "<ul>\n    <li>1</li>\n    <li>2</li>\n    <li>3</li>\n</ul>outer");
    assert_eq!(w.log, ["backup x", "repeat x", "null x", "restore x"]);

    assert_eq!(render("<p tal:repeat=\"x []\">${x}</p>|", &[]), "|");
    assert_eq!(render("<div>\n  <tal:b repeat=\"x xs\">${x}</tal:b>\n</div>", &vars), "<div>\n  123\n</div>");
    let pairs = V::List(vec![V::List(vec![V::Int(1), V::Int(2)]), V::List(vec![V::Int(3), V::Int(4)])]);
    assert_eq!(render("<p tal:repeat=\"(a, b) ps\">${a}${b}</p>", &[("ps", pairs)]), "<p>12</p>\n<p>34</p>");
}

#[test]
fn nested_repeat() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2)])), ("ys", V::List(vec![V::Int(7), V::Int(8)]))];
    assert_eq!(
        render("<div>\n  <p tal:repeat=\"x xs\">\n    <b tal:repeat=\"y ys\">${x}${y}</b>\n  </p>\n</div>", &vars),
        "<div>\n  <p>\n    <b>17</b>\n    <b>18</b>\n  </p>\n  <p>\n    <b>27</b>\n    <b>28</b>\n  </p>\n</div>"
    );
}

#[test]
fn frames_write_into_the_caller_buffer() {
    let mut w = world(&["<p a=\"${call:1}\">${call:1}|${x}</p>", "<i>${x}</i>"], &[("x", V::Str("main".into()))]);
    assert_eq!(render_world(&mut w), "<p a=\"<i>from 1</i>\"><i>from 1</i>|main</p>");
}

#[test]
fn infinite_recursion_is_an_error() {
    let mut w = world(&["${call:0}"], &[]);
    let mut st = VmState::new(Arc::clone(&w.programs[0].0), V::Code(0), V::Scope(0));
    assert!(matches!(run(&mut st, &mut w), Err(VmError::Recursion)));
}

#[test]
fn suspend_and_resume() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2)]))];
    assert_eq!(
        render(
            "<p a=\"${wait:'q'}\" tal:condition=\"wait:1\">${wait:'z'}<b tal:repeat=\"x wait:xs\">${x}</b>\
             <i tal:content=\"wait:'c'\">d</i></p>",
            &vars
        ),
        "<p a=\"q\">z<b>1</b>\n           <b>2</b><i>c</i></p>"
    );
}

#[test]
fn compile_errors() {
    let err = |src: &str| compile(src, &mut Toy::default()).unwrap_err().to_string();
    assert_eq!(err("<p>${ }</p>"), "invalid: \"\"");
    assert_eq!(err("<p tal:foo=\"x\"/>"), "Bad attribute for namespace 'http://xml.zope.org/namespaces/tal'");
    assert_eq!(err("<p tal:define=\"x 1\"/>"), "The attribute tal:define is not supported yet by chameleon-rs");
    assert_eq!(err("<p tal:repeat=\"econtext xs\"/>"), "Name disallowed by compiler: econtext");
    assert_eq!(err("<p>a</b>"), "Unexpected end tag.");
    assert_eq!(err("<!DOCTYPE html>"), "declaration is not supported yet by chameleon-rs");
    assert!(matches!(compile("${boom}", &mut Toy::default()), Err(crate::CompileError::Expr(e)) if e == "boom"));
}

#[test]
fn runtime_valued_countdown() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2), V::Int(3)]))];
    assert_eq!(render("<i tal:repeat=\"v xs\">${v}</i>", &vars), "<i>1</i>\n<i>2</i>\n<i>3</i>");
}

#[test]
fn loops_in_callee_frames_keep_their_own_unwind_entries() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2)]))];
    let mut w = world(&["<b tal:repeat=\"x xs\">${x}${call:1}${x}</b>", "<i tal:repeat=\"y [7, 8]\">${y}</i>"], &vars);
    assert_eq!(render_world(&mut w), "<b>1<i>7</i>\n<i>8</i>1</b>\n<b>2<i>7</i>\n<i>8</i>2</b>");
}

/// Runs until the first suspension and returns the state.
fn run_to_suspension(w: &mut World) -> (VmState<V>, V) {
    let mut st = VmState::new(Arc::clone(&w.programs[0].0), V::Code(0), V::Scope(0));
    match run(&mut st, w).unwrap() {
        Done::Suspended(v) => (st, v),
        Done::Finished => panic!("did not suspend"),
    }
}

#[test]
fn suspend_inside_a_callee_frame() {
    // The callee suspends in an attribute and in text; the caller's attribute closing quote
    // (on_return) is written only when the callee returns.
    let mut w = world(&["<p a=\"${call:1}\">${call:1}</p>", "<i t=\"${wait:'q'}\">${wait:'z'}</i>"], &[]);
    let (mut st, _) = run_to_suspension(&mut w);
    assert_eq!(st.frame_depth(), 2);
    let mut values = 0;
    st.for_each_value(|_| values += 1);
    assert_eq!(values, 4); // two frames, code and scope each
    st.resume_with::<String>(V::Str("q".into())).unwrap();
    let mut out = String::new();
    loop {
        match run(&mut st, &mut w).unwrap() {
            Done::Finished => break,
            Done::Suspended(v) => st.resume_with::<String>(v).unwrap(),
        }
    }
    out.push_str(std::str::from_utf8(st.out.as_bytes()).unwrap());
    assert_eq!(out, "<p a=\"<i t=\"q\">z</i>\"><i t=\"q\">z</i></p>");
}

#[test]
fn suspend_inside_a_loop_body_with_a_frame_call() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2)]))];
    let mut w = world(&["<b tal:repeat=\"x xs\">${wait:x}${call:1}</b>", "<i>${wait:x}</i>"], &vars);
    let (st, _) = run_to_suspension(&mut w);
    assert_eq!(st.unwind_depth(), 1);
    let mut values = 0;
    st.for_each_value(|_| values += 1);
    assert_eq!(values, 3); // frame code and scope, the loop iterator (no backup: `x` was unbound)
    assert_eq!(render_world(&mut w), "<b>1<i>from 1</i></b>\n<b>2<i>from 1</i></b>");
}

#[test]
fn resume_and_run_are_checked() {
    let mut w = world(&["${wait:1}"], &[]);
    let mut st = VmState::new(Arc::clone(&w.programs[0].0), V::Code(0), V::Scope(0));
    assert!(matches!(st.resume_with::<String>(V::None), Err(VmError::Internal(_))));
    assert!(matches!(run(&mut st, &mut w), Ok(Done::Suspended(_))));
    assert!(matches!(run(&mut st, &mut w), Err(VmError::Internal(_))));
    st.resume_with::<String>(V::Int(1)).unwrap();
    assert!(matches!(run(&mut st, &mut w), Ok(Done::Finished)));
}

#[test]
fn capacity_hint_is_smoothed_and_capped() {
    let p = compile("x", &mut Toy::default()).unwrap();
    p.record_size(1000);
    assert_eq!(p.capacity_hint(), 1000);
    p.record_size(usize::MAX / 2);
    assert_eq!(p.capacity_hint(), crate::MAX_CAPACITY_HINT);
    p.record_size(0);
    assert_eq!(p.capacity_hint(), crate::MAX_CAPACITY_HINT / 2);
}
