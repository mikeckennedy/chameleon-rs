//! Micro-benchmarks of the core's hot paths (plan.md, "How speed is measured"): escaping,
//! the tokenizer, and the VM loop over a toy runtime (no Python: the cost of the VM's own
//! dispatch, text, attribute and loop instructions).
//!
//! Run: `cargo bench -p chameleon-core` (numbers recorded in plan.md, Phase 6a).

use std::hint::black_box;
use std::sync::Arc;

use chameleon_core::{
    Active, Attrs, Countdown, Done, Emitted, Errors, Escape, Eval, ExprCompileError, ExprCompiler, ExprContext, ExprId,
    Fill, I18n, Loops, MacroStep, Macros, NameId, Names, Options, Output, Program, Quote, Stage, Step, Tok, Translate,
    Translation, Values, Visibility, VmState, compile, escape_into, run,
};
use criterion::{Criterion, Throughput, criterion_group, criterion_main};

/// The benchmark listing (`bench/templates/home/listing_loop.pt`) with toy expressions:
/// attribute paths instead of Python.
const LISTING: &str = r#"<div class="container videos listing">
    <span class="video" tal:repeat="video videos">
        <div>
            <a href="https://www.youtube.com/watch?v=${ video.id }" target="_blank">
                <img src="https://img.youtube.com/vi/${ video.id }/maxresdefault.jpg"
                     class="img img-responsive ${ empty }"
                     alt="${ video.title }"
                     title="${ video.title }">
            </a>
            <a href="https://www.youtube.com/watch?v=${ video.id }" target="_blank"
               class="author">${ video.author }</a>
            <div class="views">${ video.views } views</div>
        </div>
    </span>
</div>"#;

/// A toy value: strings are shared, records are small field lists (cheap to look up, so the
/// benchmark measures the VM rather than the toy).
#[derive(Clone, Debug)]
enum V {
    None,
    Str(Arc<str>),
    List(Arc<[V]>),
    Record(Arc<[(&'static str, V)]>),
    Iter(Arc<[V]>),
    Unit,
}

/// Expressions are dotted paths (`video.id`).
#[derive(Default, Debug)]
struct Paths {
    exprs: Vec<Vec<String>>,
}

impl ExprCompiler for Paths {
    type Error = String;

    fn compile(&mut self, source: &Tok, _cx: ExprContext<'_>) -> Result<ExprId, ExprCompileError<String>> {
        let s = source.text.trim();
        if s.is_empty() || !s.chars().all(|c| c.is_alphanumeric() || c == '.' || c == '_') {
            return Err(ExprCompileError::Invalid { message: "invalid".into(), token: None });
        }
        self.exprs.push(s.split('.').map(str::to_owned).collect());
        Ok(ExprId(u32::try_from(self.exprs.len() - 1).expect("few expressions")))
    }

    fn compile_code(&mut self, _source: &Tok) -> Result<ExprId, ExprCompileError<String>> {
        Err(ExprCompileError::Other("no code blocks".into()))
    }

    fn deferred(&mut self, message: &str, _token: &Tok) -> Result<ExprId, ExprCompileError<String>> {
        Err(ExprCompileError::Other(message.into()))
    }
}

#[derive(Debug)]
struct Toy {
    program: Arc<Program>,
    exprs: Vec<Vec<String>>,
    /// A few variables: a linear search is faster than hashing.
    scope: Vec<(String, V)>,
    position: usize,
}

impl Toy {
    fn name(&self, n: NameId) -> &str {
        &self.program.names()[n.0 as usize]
    }

    fn get(&self, name: &str) -> Option<&V> {
        self.scope.iter().find(|(k, _)| k == name).map(|(_, v)| v)
    }

    fn set(&mut self, name: &str, value: Option<V>) {
        let at = self.scope.iter().position(|(k, _)| k == name);
        match (at, value) {
            (Some(i), Some(v)) => self.scope[i].1 = v,
            (None, Some(v)) => self.scope.push((name.to_owned(), v)),
            (Some(i), None) => {
                self.scope.swap_remove(i);
            }
            (None, None) => {}
        }
    }
}

impl Values for Toy {
    type Value = V;
    type Error = String;

    fn clone_value(&mut self, value: &V) -> V {
        value.clone()
    }
}

impl Eval for Toy {
    fn activate(&mut self, _frame: Active<'_, V>) -> Result<(), String> {
        Ok(())
    }

    fn eval(&mut self, expr: ExprId, _emitted: bool) -> Result<Step<V>, String> {
        let path = &self.exprs[expr.0 as usize];
        let mut value = self.get(&path[0]).cloned().ok_or("NameError")?;
        for part in &path[1..] {
            value = match value {
                V::Record(r) => r.iter().find(|(k, _)| k == part).map(|(_, v)| v.clone()).ok_or("AttributeError")?,
                _ => return Err("AttributeError".into()),
            };
        }
        Ok(Step::Ready(value))
    }

    fn emit(&mut self, value: V, esc: Escape, _stage: Stage, out: &mut Output) -> Result<Emitted, String> {
        match value {
            V::Str(s) => {
                escape_into(s.as_bytes(), esc, out);
                Ok(Emitted::Value)
            }
            _ => Ok(Emitted::Nothing),
        }
    }

    fn is_default(&mut self, _value: &V) -> bool {
        false
    }

    fn truthy(&mut self, value: &V) -> Result<bool, String> {
        Ok(!matches!(value, V::None))
    }

    fn is_false(&mut self, _value: &V) -> bool {
        false
    }
}

impl Names for Toy {
    fn lookup(&mut self, name: NameId) -> Result<Option<V>, String> {
        Ok(self.get(self.name(name)).cloned())
    }

    fn set_null(&mut self, name: NameId) -> Result<(), String> {
        let n = self.name(name).to_owned();
        self.set(&n, Some(V::None));
        Ok(())
    }

    fn define(&mut self, names: &[NameId], value: V, visibility: Visibility) -> Result<(), String> {
        self.assign(names, value, visibility)
    }

    fn restore(&mut self, name: NameId, backup: Option<V>) -> Result<(), String> {
        let n = self.name(name).to_owned();
        self.set(&n, backup);
        Ok(())
    }

    fn text_value(&mut self, _text: Option<&str>) -> V {
        V::None
    }
}

impl Loops for Toy {
    fn repeat(&mut self, _names: &[NameId], iterable: V) -> Result<(V, Countdown<V>), String> {
        let V::List(items) = iterable else { return Err("TypeError".into()) };
        let len = i64::try_from(items.len()).expect("short list");
        Ok((V::Iter(items), Countdown::Int(len)))
    }

    fn countdown(&mut self, _index: &V) -> Result<(V, bool), String> {
        Err("unused".into())
    }

    fn next(&mut self, iterator: &V) -> Result<Option<V>, String> {
        let V::Iter(items) = iterator else { return Err("TypeError".into()) };
        // One loop: the position lives in the runtime (values are shared, not mutable).
        self.position += 1;
        Ok(items.get(self.position - 1).cloned())
    }

    fn assign(&mut self, names: &[NameId], item: V, _visibility: Visibility) -> Result<(), String> {
        let at = self.scope.iter().position(|(k, _)| **k == *self.program.names()[names[0].0 as usize]);
        if let Some(i) = at {
            self.scope[i].1 = item;
        } else {
            let n = self.name(names[0]).to_owned();
            self.scope.push((n, item));
        }
        Ok(())
    }

    fn missing_backup(&mut self, _name: NameId) -> String {
        "NameError".into()
    }
}

impl Errors for Toy {
    fn recursion_error(&mut self) -> String {
        "RecursionError".into()
    }
}

impl Attrs for Toy {
    fn yields_name(&mut self, _container: &V, _name: &str) -> Result<bool, String> {
        Ok(false)
    }

    fn attr_dict(&mut self, _dict: &V, _ex: &[&str], _bools: &[&str], _out: &mut Output) -> Result<(), String> {
        Ok(())
    }

    fn case_compare(&mut self, _value: &V, _switch: Option<&V>) -> Result<V, String> {
        Ok(V::Unit)
    }
}

/// The listing has no macros or translations.
impl Macros for Toy {
    fn macro_call(&mut self, _value: V, _out: &mut Output) -> Result<MacroStep<V>, String> {
        Err("unused".into())
    }

    fn copy_scope(&mut self) -> Result<V, String> {
        Err("unused".into())
    }

    fn after_macro(&mut self) -> Result<(), String> {
        Ok(())
    }

    fn fill_slot(&mut self, _name: &str, _fill: Fill<V>, _extend: bool) -> Result<(), String> {
        Err("unused".into())
    }

    fn pop_slot(&mut self, _name: &str) -> Option<V> {
        None
    }

    fn slot_call(&mut self, _slot: &V, _out: &mut Output) -> Result<Option<Fill<V>>, String> {
        Err("unused".into())
    }
}

impl Translate for Toy {
    fn i18n_changed(&mut self, _i18n: &I18n<V>) -> Result<(), String> {
        Ok(())
    }

    fn translate(&mut self, _call: Translation<'_, V>) -> Result<V, String> {
        Err("unused".into())
    }

    fn quote_value(&mut self, value: V, _esc: Escape, _default: Option<&str>) -> Result<V, String> {
        Ok(value)
    }

    fn convert_value(&mut self, value: V) -> Result<V, String> {
        Ok(value)
    }
}

fn videos(n: usize) -> V {
    let items: Vec<V> = (0..n)
        .map(|i| {
            let s = |v: String| V::Str(Arc::from(v));
            V::Record(Arc::new([
                ("id", s(format!("v{i}"))),
                ("title", s(format!("Title {i} & \"<b>\""))),
                ("author", s(format!("Author {}", i % 3))),
                ("views", s(format!("{}", 1_000_000 - i * 37))),
            ]))
        })
        .collect();
    V::List(Arc::from(items))
}

fn bench_escape(c: &mut Criterion) {
    let mut group = c.benchmark_group("escape");
    let plain = "The quick brown fox jumps over the lazy dog. ".repeat(23);
    let dense = "a < b && c > \"d\" 'e' ".repeat(50);
    let short = "Title 42";
    for (name, text, esc) in [
        ("plain_1k_text", plain.as_str(), Escape::Text),
        ("dense_1k_attr", dense.as_str(), Escape::Attr(Quote::Double)),
        ("short_attr", short, Escape::Attr(Quote::Double)),
    ] {
        group.throughput(Throughput::Bytes(text.len() as u64));
        group.bench_function(name, |b| {
            let mut out = Output::with_capacity(4 * text.len());
            b.iter(|| {
                out.truncate(chameleon_core::Mark::default());
                escape_into(black_box(text.as_bytes()), esc, &mut out);
                black_box(out.len())
            });
        });
    }
    group.finish();
}

fn bench_tokenizer(c: &mut Criterion) {
    let source =
        include_str!("../../../tests/upstream/chameleon_tests/inputs/031-namespace-with-tal.pt").repeat(40) + LISTING;
    let mut group = c.benchmark_group("tokenizer");
    group.throughput(Throughput::Bytes(source.len() as u64));
    group.bench_function("tokenize", |b| b.iter(|| black_box(chameleon_core::testing::tokenize(black_box(&source)))));
    group.finish();
}

fn bench_vm(c: &mut Criterion) {
    let mut paths = Paths::default();
    let program = Arc::new(compile(LISTING, &mut paths, &Options::default()).expect("the listing compiles"));
    let n = 500;
    let items = videos(n);
    let mut group = c.benchmark_group("vm");
    group.throughput(Throughput::Elements(n as u64));
    group.bench_function("listing_500_items", |b| {
        let mut toy = Toy {
            program: Arc::clone(&program),
            exprs: paths.exprs.clone(),
            scope: vec![("videos".to_owned(), items.clone()), ("empty".to_owned(), V::None)],
            position: 0,
        };
        b.iter(|| {
            toy.position = 0;
            let mut st = VmState::new(Arc::clone(&program), V::Unit, V::Unit, None);
            match run(&mut st, &mut toy) {
                Ok(Done::Finished) => {}
                other => panic!("{other:?}"),
            }
            black_box(st.out.len())
        });
    });
    group.finish();
}

criterion_group!(benches, bench_escape, bench_tokenizer, bench_vm);
criterion_main!(benches);
