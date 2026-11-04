from datetime import datetime, timedelta, timezone

from app.models import ModelHit, license_allows_commercial, parse_date
from app.ranking import Profile, apply_filters, dedupe, facets, normalize_weights, rank, relevance, sort_hits

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def hit(source="a", title="x", pos=0, likes=None, downloads=None, days=None, author=None, price=None,
        tags=None, makes=None, commercial=None, thumb="t"):
    return ModelHit(
        source=source, id=f"{source}-{title}-{pos}", title=title, url=f"https://{source}/{pos}",
        likes=likes, downloads=downloads, author=author, price=price, tags=tags or [], position=pos,
        makes=makes, commercial=commercial, thumbnail=thumb,
        published=(NOW - timedelta(days=days)) if days is not None else None,
    )


def test_relevance_prefers_matching_title():
    q = "headphone holder"
    assert relevance(q, hit(title="Headphone Holder Clamp")) > relevance(q, hit(title="Cable clip"))
    assert relevance(q, hit(title="headphones holders")) > 0.5


def test_relevance_handles_cyrillic():
    assert relevance("держатель наушников", hit(title="Держатель для наушников")) > 0.6


def test_weights_are_normalized():
    w = normalize_weights({"relevance": 2, "popularity": 2})
    assert w["relevance"] == 0.5 and w["popularity"] == 0.5 and w["semantic"] == 0
    assert set(normalize_weights({"relevance": -3}).values()) == {0.0}


def test_popularity_is_normalized_per_source():
    hits = [hit("big", "m1", likes=1_000_000), hit("big", "m2", likes=1000),
            hit("small", "m3", likes=1000), hit("small", "m4", likes=1)]
    ranked = rank(hits, "zzz", {"popularity": 1}, {}, do_dedupe=False, now=NOW)
    pops = {h.title: h.breakdown["popularity"] for h in ranked}
    assert pops["m1"] == 1.0 and pops["m3"] == 1.0 and pops["m2"] < 1.0


def test_makes_count_into_popularity():
    hits = [hit("a", "p1", likes=10, makes=50), hit("a", "p2", likes=10, makes=0)]
    ranked = rank(hits, "zzz", {"popularity": 1}, {}, do_dedupe=False, now=NOW)
    assert ranked[0].title == "p1"


def test_source_weight_multiplies_score():
    hits = [hit("a", "same thing"), hit("b", "same thing other")]
    ranked = rank(hits, "same thing", {"relevance": 1}, {"a": 0.1, "b": 1.0}, do_dedupe=False, now=NOW)
    assert ranked[0].source == "b"


def test_semantic_signal_used_only_when_given():
    hits = [hit("a", "Держатель", pos=0), hit("a", "Lamp", pos=1)]
    sem = {hits[1].key: 1.0, hits[0].key: 0.0}
    ranked = rank(hits, "lamp shade", {"semantic": 1, "relevance": 0}, {}, do_dedupe=False, now=NOW, semantic=sem)
    assert ranked[0].title == "Lamp" and "semantic" in ranked[0].breakdown
    ranked = rank(hits, "lamp shade", {"semantic": 1, "relevance": 1}, {}, do_dedupe=False, now=NOW)
    assert "semantic" not in ranked[0].breakdown


def test_alt_query_counts_for_relevance():
    h = hit(title="Headphone holder")
    plain = rank([h.copy()], "держатель наушников", {"relevance": 1}, {}, now=NOW)[0].score
    alt = rank([h.copy()], "держатель наушников", {"relevance": 1}, {}, now=NOW, alt_query="headphone holder")[0].score
    assert alt > plain


def test_personal_profile_boost():
    fav = [hit("a", "Gridfinity bin", author="zack", tags=["gridfinity"])]
    prof = Profile.from_hits(fav)
    a = hit("b", "Gridfinity tool tray", author="zack")
    b = hit("b", "Random vase", author="other", pos=0)
    ranked = rank([b, a], "zzz", {"personal": 1}, {}, do_dedupe=False, now=NOW, profile=prof)
    assert ranked[0] is a and a.breakdown["personal"] > 0.5


def test_freshness_weight_moves_new_models_up():
    hits = [hit("a", "lamp", days=3000), hit("b", "lamp", days=10)]
    ranked = rank(hits, "lamp", {"freshness": 1}, {}, do_dedupe=False, now=NOW)
    assert ranked[0].source == "b" and ranked[0].breakdown["freshness"] > 0.9


def test_dedupe_merges_same_model_across_sites_only():
    hits = [hit("a", "Headphone Holder Clamp", author="anna", likes=10),
            hit("b", "headphone holder clamp", author="Anna", likes=999),
            hit("a", "Headphone Holder Clamp", author="anna", pos=1)]
    for i, h in enumerate(hits):
        h.score = 1.0 - i * 0.1
    out = dedupe(hits)
    assert len(out) == 2 and out[0].also_on == [{"source": "b", "url": "https://b/0", "key": hits[1].key}]


def test_dedupe_keeps_different_authors_apart():
    assert len(dedupe([hit("a", "Benchy boat test", author="x"), hit("b", "Benchy boat test", author="y")])) == 2


def test_filters():
    hits = [hit(title="Cool NSFW thing"), hit(title="paid", price=5), hit(title="free", price=0),
            hit(title="unknown"), hit(title="few likes", likes=1, price=0)]
    out = apply_filters(hits, {"exclude": ["nsfw"], "price": "free"})
    assert [h.title for h in out] == ["free", "unknown", "few likes"]
    assert apply_filters(hits, {"min_likes": 5}) == []
    assert [h.title for h in apply_filters(hits, {"price": "paid"})] == ["paid"]
    assert [h.title for h in apply_filters(hits, {"free_only": True})] == ["Cool NSFW thing", "free", "unknown", "few likes"]
    assert [h.title for h in apply_filters(hits, {"max_price": 3})] != [] and \
        "paid" not in [h.title for h in apply_filters(hits, {"max_price": 3})]


def test_filters_dates_license_words_authors_images():
    hits = [hit(title="Old lamp", days=400, commercial=True, author="ann"),
            hit(title="New lamp shade", days=5, commercial=False, author="bob"),
            hit(title="Lamp no date", commercial=None, thumb=None, author="cid")]
    assert [h.title for h in apply_filters(hits, {"days": 30}, now=NOW)] == ["New lamp shade"]
    assert [h.title for h in apply_filters(hits, {"license": "commercial"})] == ["Old lamp", "Lamp no date"]
    assert [h.title for h in apply_filters(hits, {"license": "commercial_strict"})] == ["Old lamp"]
    assert [h.title for h in apply_filters(hits, {"include": "shade"})] == ["New lamp shade"]
    assert [h.title for h in apply_filters(hits, {"include": ["lamp shade"]})] == ["New lamp shade"]
    assert [h.title for h in apply_filters(hits, {"exclude_authors": "ANN, bob"})] == ["Lamp no date"]
    assert [h.title for h in apply_filters(hits, {"has_image": True})] == ["Old lamp", "New lamp shade"]
    assert [h.title for h in apply_filters(hits, {}, hidden={hits[0].key})] == ["New lamp shade", "Lamp no date"]
    assert [h.title for h in apply_filters(hits, {"hide_seen": True}, seen={hits[1].key})] == ["Old lamp", "Lamp no date"]


def test_facets_split_ambiguous_query():
    titles = (["Hollow Knight Hornet figure", "Hornet Hollow Knight Silksong", "Hornet - Hollow Knight diorama",
               "Hollow Knight Hornet bust"] +
              ["F/A-18 Super Hornet kit card", "Super Hornet jet", "F/A-18 Super Hornet micro"] +
              ["Asian hornet trap", "Hornet trap 2023", "Wasp & Hornet trap bottle"])
    hits = [hit("a", t, pos=i) for i, t in enumerate(titles)]
    terms = [f["term"] for f in facets(hits, "hornet")]
    assert "hollow knight" in terms and "super hornet" in terms and any("trap" in t for t in terms)
    assert "super" not in terms
    assert "hornet" not in terms
    assert "hollow" not in terms and "knight" not in terms


def test_sort_modes():
    hits = [hit(title="old", days=900, likes=5, makes=1), hit(title="new", days=1, likes=1, makes=9),
            hit(title="nodate", likes=9)]
    assert [h.title for h in sort_hits(hits, "newest")] == ["new", "old", "nodate"]
    assert [h.title for h in sort_hits(hits, "likes")] == ["nodate", "old", "new"]
    assert sort_hits(hits, "makes")[0].title == "new"


def test_license_heuristic():
    assert license_allows_commercial("CC BY-NC-SA") is False
    assert license_allows_commercial("Creative Commons - Attribution") is None or True
    assert license_allows_commercial("CC BY") is True
    assert license_allows_commercial("CC0 Public Domain") is True
    assert license_allows_commercial("Standard Digital File License") is None
    assert license_allows_commercial(None) is None


def test_license_does_not_match_inside_words():
    for name in ("No commercial use", "Commercial use prohibited", "Commercial use not permitted",
                 "CC-BY-NC-SA", "Attribution-NonCommercial-ShareAlike", "Personal Use", "Private use only"):
        assert license_allows_commercial(name) is False, name
    for name in ("CC-BY", "CC BY-SA", "CC0", "GPL 2.0", "GNU - LGPL", "MIT License", "BSD License",
                 "Creative Commons Attribution licence 4.0", "Creative Commons - Attribution", "Commercial use allowed"):
        assert license_allows_commercial(name) is True, name
    for name in ("Limited license", "All rights reserved", "Standard Digital File", "Permitted for friends"):
        assert license_allows_commercial(name) is None, name


def test_exclude_matches_whole_words_and_forms():
    titles = ["Fastest benchy", "Latest gear", "Contest trophy", "Test cube", "Tests pack", "Testing rig",
              "Shopping cart", "Smart plug", "Card holder", "Catapult", "Location tag"]
    def left(word):
        return {h.title for h in apply_filters([hit(title=t) for t in titles], {"exclude": [word]})}
    assert set(titles) - left("test") == {"Test cube", "Tests pack", "Testing rig"}
    assert set(titles) - left("art") == set()
    assert set(titles) - left("cat") == set()
    assert set(titles) - left("cart") == {"Shopping cart"}


def test_include_does_not_confuse_similar_words():
    titles = ["Cast mold", "Phone case", "Phone cases", "Bold font", "M3 Bolt", "M3 bolts", "Benchy", "Bench vise"]
    def kept(word):
        return {h.title for h in apply_filters([hit(title=t) for t in titles], {"include": [word]})}
    assert kept("case") == {"Phone case", "Phone cases"}
    assert kept("bolt") == {"M3 Bolt", "M3 bolts"}
    assert kept("benchy") == {"Benchy"}
    assert relevance("bolt", hit(title="Bold font")) < 0.3


def test_word_forms_still_match():
    from app.ranking import stem
    for a, b in (("holder", "holders"), ("gear", "gears"), ("box", "boxes"), ("mount", "mounted"), ("glass", "glass"),
                 ("string", "strings"), ("headphone", "headphones"), ("наушники", "наушников"), ("держатель", "держателя")):
        assert stem(a) == stem(b), (a, b)
    for a, b in (("case", "cast"), ("bolt", "bold"), ("card", "cart"), ("bench", "benchy")):
        assert stem(a) != stem(b), (a, b)


def test_matches_query_needs_half_of_the_words():
    from app.ranking import matches_query
    assert matches_query("phone stand", hit(title="Orbit Phone Holder"))
    assert matches_query("gridfinity", hit(title="SSD holder", tags=["Gridfinity"]))
    assert not matches_query("benchy", hit(title="Quick-Release Bench Vise"))
    assert matches_query("держатель", hit(title="Headphone holder"), alt_query="holder")
    assert matches_query("!!", hit(title="Anything"))


def test_parse_date_variants():
    assert parse_date("2025-01-01T00:00:00Z").year == 2025
    assert parse_date(1735689600000).year == 2025
    assert parse_date("1735689600").year == 2025
    assert parse_date("garbage") is None and parse_date(None) is None
