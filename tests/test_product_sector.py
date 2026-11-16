"""Γενική κατηγορία προϊόντος: η αλλαγή αποθηκεύεται, φαίνεται στη λίστα, και «Όλα τα καταστήματα» υπερισχύει
της γενικής κατηγορίας της κατηγορίας του."""
from pricetracker.db import Database
from pricetracker.web import create_app


class FW:
    status = {"running": False}
    health = {}

    def enqueue(self, *a, **k):
        return True

    def pending(self):
        return 0


def test_change_product_sector(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FW(), tmp_path).test_client()
    elec = db.create_sector("Τεστ Ηλεκτρικά")
    clothes = db.create_sector("Τεστ Ρούχα")
    cat = db.create_category("Κουτιά")
    db.set_category_sector(cat, elec)
    pid = db.create_product("Lian Li O11", cat)

    def listed():
        return next(p for p in c.get("/api/products").get_json() if p["id"] == pid)

    assert listed()["sector_name"] == "Τεστ Ηλεκτρικά"  # από την κατηγορία, και φαίνεται στη λίστα
    c.put(f"/api/products/{pid}", json={"sector_id": clothes})
    assert listed()["sector_name"] == "Τεστ Ρούχα" and db.product_sector(pid) == clothes
    c.put(f"/api/products/{pid}", json={"sector_id": "all"})
    assert listed()["sector_name"] is None and db.product_sector(pid) is None
    assert c.get(f"/api/products/{pid}").get_json()["all_shops"] == 1
    c.put(f"/api/products/{pid}", json={"sector_id": ""})  # ξανά όπως η κατηγορία
    assert listed()["sector_name"] == "Τεστ Ηλεκτρικά" and db.product_sector(pid) == elec


def test_inherit_follows_category_later(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FW(), tmp_path).test_client()
    cat = db.create_category("Χωρίς γενική")
    pid = db.create_product("P", cat)
    c.put(f"/api/products/{pid}", json={"name": "P2", "sector_id": ""})  # η προεπιλογή της φόρμας
    sec = db.create_sector("Τεστ Εργαλεία")
    db.set_category_sector(cat, sec)
    assert db.product_sector(pid) == sec and db.get_product(pid)["sector_name"] == "Τεστ Εργαλεία"
