"""Pure unit test for the food-search relevance ranking (no DB, no network).
Extracts _food_rank from dashboard.py so the test doesn't import the whole app."""
import ast, pathlib, sys
src = (pathlib.Path(__file__).resolve().parent.parent / "dashboard" / "dashboard.py").read_text()
node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_food_rank")
ns = {}
exec(compile(ast.Module([node], []), "dashboard.py", "exec"), ns)
rank = ns["_food_rank"]
cases = [
    (("Chicken breast", None, "chicken breast"), 0),
    (("Chicken breast fillets", None, "Chicken"), 1),          # mixed case, prefix
    (("Grilled chicken breast", None, "chick"), 2),             # word-prefix inside the name
    (("6 Chicken McNuggets", None, "chick"), 2),
    (("McChicken", None, "chick"), 3),                          # contained mid-word
    (("Breast, Chicken", None, "chicken breast"), 2),           # words in any order
    (("Greek Yogurt", "Chobani", "choba"), 2),                  # brand match counts
    (("Witte bollen", None, "mcc"), 9),                         # external junk
    (("Egg", None, "eg"), 1),                                   # 2-letter term
]
bad = [(c, rank(*c[0]), c[1]) for c in cases if rank(*c[0]) != c[1]]
for c, got, want in bad:
    print("FAIL", c[0], "got", got, "want", want)
print(f"{len(cases) - len(bad)}/{len(cases)} ranking cases pass")
sys.exit(1 if bad else 0)
