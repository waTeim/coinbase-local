# Simple Coinbase Pro Orderbook proxy with added feature(s)

This server uses the coinbase-pro orderbook sync object to maintain a local copy of
the orderbook for the specified products.  The coinbase pro API is missing aggregation
on the level 3 feed, this provides that.


## To build 

    docker build -t cb_local .

## To Run

The server takes as parameters a list of products on the command line, the docker uses
the `$PRODUCTS` env variable to provide that.  Internally, the server binds to port
63200, so to use externally that should be mapped.

For example

    docker run --rm -d -p 6300:63200 --name cb_local --env PRODUCTS=BTC-USD:ETH-USD cb_local

Given the above, once running swaggar docs are available [locally](http://localhost:6300/api/docs).

## Notes

The orderbook is synchronized upon the first invocation of a query to it.  The sync ususally takes
2-5 seconds and so the response to very fist call will be delayed.  After that calls to `/api/orderBook/interval`
respond very quickly.
