from core import db
import web

class SQL:
    def get_modified_pages(self, url, user_id):
        site_id = db.get_site_id(url)

        #@@ improve later
        d = web.query("""
            SELECT 
                page.id as id,
                page.path as path,
                MAX(version.revision) as revision, 
                MAX(review.revision) as reviewed_revision
            FROM page
            JOIN version ON page.id = version.page_id
            LEFT OUTER JOIN review 
                ON page.id = review.page_id
                AND review.user_id=$user_id
            GROUP BY page.id, page.path
            """, vars=locals())

        d = [p for p in d if not p.reviewed_revision or p.revision > p.reviewed_revision]
        return d

    def approve(self, url, user_id, path, revision):
        import core
        site_id = core.db.get_site_id(url)
        page_id = core.db.get_page_id(url, path)

        #@@ is there any better way?
        web.transact()
        try:
            web.delete('review', where="site_id=$site_id AND page_id=$page_id AND user_id=$user_id", vars=locals())
            web.insert('review', site_id=site_id, page_id=page_id, user_id=user_id, revision=revision)
        except:
            web.rollback()
            raise
        else:
            web.commit()

from utils.delegate import pickdb
pickdb(globals())
