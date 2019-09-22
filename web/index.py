import json

import tornado.httpclient
import tornado.ioloop
import tornado.routing
import tornado.web
from bs4 import BeautifulSoup
from scrapy.selector import Selector

from discovery.spiders.ncbi_geo import NCBIGeoSpider


class MainHandler(tornado.web.RequestHandler):

    async def get(self):

        url = 'https://www.ncbi.nlm.nih.gov' + self.request.uri
        http_client = tornado.httpclient.AsyncHTTPClient()
        response = await http_client.fetch(url)

        if self.request.path == '/geo/query/acc.cgi':
            text = response.body.decode()
            soup = BeautifulSoup(text, 'html.parser')
            doc = NCBIGeoSpider().parse(Selector(text=text))
            new_tag = soup.new_tag('script', type="application/ld+json")
            new_tag.string = json.dumps(doc, indent=4)
            soup.head.insert(0, new_tag)
            self.write(soup.prettify())
        else:
            self.write(response.body)


if __name__ == "__main__":
    application = tornado.web.Application([
        (tornado.routing.AnyMatches(), MainHandler),
    ])
    application.listen(8080)
    tornado.ioloop.IOLoop.current().start()