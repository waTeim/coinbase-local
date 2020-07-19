
import { ControllerBase, ControllerProperties, get, post, controller, Res } from 'ts-api';
import { Orderbook } from '../lib/Orderbook';
import { start } from 'repl';

const BigNumber = require('bignumber.js');


interface OrderbookEntry
{
  price:number;
  size:number;
  numOrders:number;
};

interface OrderbookInterval
{
  aggregation:number;
  depth:number;
  date:Date;
  sequence:number;
  midpoint:string;
  asks:any[][];
  bids:any[][];
};

interface MarketOrder
{
  price:number;
  size:number;
  numOrders:number;
};

interface MarketOrderInterval
{
  sequence:number;
  buy:MarketOrder;
  sell:MarketOrder;
};

/**
 * Query the orderbook
 */
@controller('/orderBook')
export default class QueryManager extends ControllerBase
{
  static book:Orderbook;

  constructor(properties:ControllerProperties)
  {
    super(properties);
  }

  @get('/interval') async getInterval(product:string,aggregation:number,depth:number):Promise<OrderbookInterval>
  {
    if(QueryManager.book == null) 
    {
      QueryManager.book = new Orderbook(this.properties.context.products);
      await QueryManager.book.init();
      console.log("book is synced");
    }

    let asks:OrderbookEntry[] = [];
    let bids:OrderbookEntry[] = [];
    let now = new Date();
    let zero = BigNumber(0);
    let state = QueryManager.book.state(product);
    let minAsk = state.asks[0].price;
    let maxBid = state.bids[0].price;
    let agg = BigNumber(aggregation);
    const midpointPrice = minAsk.plus(maxBid).dividedBy(2);
    let sequence = QueryManager.book.sequence(product);

    if(aggregation == 0)
    {
      let askArray:any[][] = [];
      let bidArray:any[][] = [];
      let bidSize = BigNumber(0);
      let bidPrice;
      let bidOrders;
      let askSize = BigNumber(0);
      let askPrice;
      let askOrders;

      //console.log(`midpoint = ${midpointPrice.toNumber()}`);

      for(let index = 0;asks.length < depth;index++)
      {
        if(index == 0 || state.asks[index].price.gt(state.asks[index - 1].price))
        {
          if(index != 0) asks.push({ price:askPrice, size:askSize, numOrders:askOrders });
          askPrice = BigNumber(state.asks[index].price);
          askSize = BigNumber(state.asks[index].size);
          askOrders = 1;
        }
        else
        {
          askSize = askSize.plus(state.asks[index].size);
          askOrders++;
        }
      }

      for(let index = 0;bids.length < depth;index++)
      {
        if(index == 0 || state.bids[index].price.lt(state.bids[index - 1].price))
        {
          if(index != 0) bids.push({ price:bidPrice, size:bidSize, numOrders:bidOrders });
          bidPrice = BigNumber(state.bids[index].price);
          bidSize = BigNumber(state.bids[index].size);
          bidOrders = 1;
        }
        else
        {
          bidSize = bidSize.plus(state.bids[index].size);
          bidOrders++;
        }
      }

      for(let i = 0;i < depth;i++) askArray.push([asks[i].price.toString(),asks[i].size.toString(),asks[i].numOrders]);
      for(let i = 0;i < depth;i++) bidArray.push([bids[i].price.toString(),bids[i].size.toString(),bids[i].numOrders]);

      return { aggregation:aggregation, depth:depth, date:now, midpoint:midpointPrice.toString(), sequence:sequence, asks:askArray, bids:bidArray }
    }

    const askAnchor = minAsk.dividedBy(agg).decimalPlaces(0,BigNumber.ROUND_UP);
    const bidAnchor = maxBid.dividedBy(agg).decimalPlaces(0,BigNumber.ROUND_DOWN);
    let discriminators:any = [[],[]];

    //console.log(`midpoint = ${midpointPrice.toNumber()} bid anchor = ${bidAnchor.toNumber()} ask anchor = ${askAnchor.toNumber()}`);

    for(let i = 0;i < depth;i++) 
    {
      discriminators[0].push(bidAnchor.minus(i).multipliedBy(agg));
      discriminators[1].push(askAnchor.plus(i).multipliedBy(agg));
    }

    let sizeSum = BigNumber(0);
    let priceSum = BigNumber(0);
    let numOrders = 0;
    let currentBucket = 0;
    let duplicateCheck:any = {};

    for(let bidIndex = 0;state.bids[bidIndex].price.gt(discriminators[0][depth - 1]);bidIndex++)
    {
      let id:string = state.bids[bidIndex].id;

      if(duplicateCheck[id] != null) continue;
      duplicateCheck[id] = true;

      let price = state.bids[bidIndex].price;
      let size = state.bids[bidIndex].size;

      if(price.lt(discriminators[0][currentBucket]))
      {
        let bucketPrice;
        
        if(priceSum.gt(zero)) bucketPrice = priceSum.dividedBy(sizeSum);
        else bucketPrice = discriminators[0][currentBucket].plus(discriminators[0][currentBucket - 1]).dividedBy(2);
        bids.push({ price:bucketPrice.toNumber(), size:sizeSum.toNumber(), numOrders:numOrders });
        sizeSum = BigNumber(0);
        priceSum = BigNumber(0);
        numOrders = 0;
        currentBucket++;
      }
      sizeSum = sizeSum.plus(size);
      priceSum = priceSum.plus(price.multipliedBy(size));
      numOrders++;
    }
    bids.push({ price:priceSum.dividedBy(sizeSum).toNumber(), size:sizeSum.toNumber(), numOrders:numOrders });

    sizeSum = BigNumber(0);
    priceSum = BigNumber(0);
    numOrders = 0;
    currentBucket = 0;
    duplicateCheck = {}

    for(let askIndex = 0;state.asks[askIndex].price.lt(discriminators[1][depth - 1]);askIndex++)
    {
      let id:string = state.asks[askIndex].id;

      if(duplicateCheck[id] != null) continue;
      duplicateCheck[id] = true;

      let price = state.asks[askIndex].price;
      let size = state.asks[askIndex].size;

      //if(currentBucket == 0) console.log(state.asks[askIndex],price.toNumber(),",",size.toNumber());

      if(price.gt(discriminators[1][currentBucket]))
      {
        let bucketPrice;

        if(priceSum.gt(zero)) bucketPrice = priceSum.dividedBy(sizeSum);
        else bucketPrice = discriminators[1][currentBucket].plus(discriminators[1][currentBucket - 1]).dividedBy(2);
        asks.push({ price:bucketPrice.toNumber(), size:sizeSum.toNumber(), numOrders:numOrders });
        sizeSum = BigNumber(0);
        priceSum = BigNumber(0);
        numOrders = 0;
        currentBucket++;
      }
      sizeSum = sizeSum.plus(size);
      priceSum = priceSum.plus(price.multipliedBy(size));
      numOrders++;
    }
    asks.push({ price:priceSum.dividedBy(sizeSum).toNumber(), size:sizeSum.toNumber(), numOrders:numOrders });

    let askArray:any[][] = [];
    let bidArray:any[][] = [];

    for(let i = 0;i < asks.length;i++) askArray.push([asks[i].price.toString(),asks[i].size.toString(),asks[i].numOrders]);
    for(let i = 0;i < bids.length;i++) bidArray.push([bids[i].price.toString(),bids[i].size.toString(),bids[i].numOrders]);

    return { aggregation:aggregation, depth:depth, date:now, midpoint:midpointPrice.toString(), sequence:sequence, asks:askArray, bids:bidArray };
  }

  @get('/marketOrders')  async getMarketOrderInterval(product:string,since?:number):Promise<MarketOrderInterval>
  {
    if(QueryManager.book == null) 
    {
      QueryManager.book = new Orderbook(this.properties.context.products);
      await QueryManager.book.init();
      console.log("book is synced");
    }

    let orders = QueryManager.book.circularBufferArray(product);

    if(since == null) 
    {
      let sequence = QueryManager.book.sequence(product);

      return { sequence:sequence, buy:{ price:0, size:0, numOrders:0 }, sell:{ price:0, size:0, numOrders:0 }};
    }
    else
    {
      let buyPriceSum = BigNumber(0);
      let buySizeSum = BigNumber(0);
      let buyNumOrders = 0;
      let sellPriceSum = BigNumber(0);
      let sellSizeSum = BigNumber(0);
      let sellNumOrders = 0;
      let buy = BigNumber(0);
      let sell = BigNumber(0);
      let sequence;

      for(let i = 0;i < orders.length && orders[i].sequence > since;i++)
      {
        let size = BigNumber(orders[i].size);
        let price = BigNumber(orders[i].price);

        if(sequence == null) sequence = orders[i].sequence;
        if(orders[i].side == "buy")
        {
          buyPriceSum = buyPriceSum.plus(price.multipliedBy(size));
          buySizeSum = buySizeSum.plus(size);
          buyNumOrders += 1;
        }
        if(orders[i].side == "sell")
        {
          sellPriceSum = sellPriceSum.plus(price.multipliedBy(size));
          sellSizeSum = sellSizeSum.plus(size);
          sellNumOrders += 1;
        }
      }
      if(sequence == null) sequence = QueryManager.book.sequence(product);
      if(buySizeSum.gt(BigNumber(0))) buy = buyPriceSum.dividedBy(buySizeSum);
      if(sellSizeSum.gt(BigNumber(0))) sell = sellPriceSum.dividedBy(sellSizeSum);

      return { 
        sequence:sequence, 
        buy:{ price:buy.toNumber(), size:buySizeSum.toNumber(), numOrders:buyNumOrders }, 
        sell:{ price:sell.toNumber(), size:sellSizeSum.toNumber(), numOrders:sellNumOrders },
       };
    }
  }
}