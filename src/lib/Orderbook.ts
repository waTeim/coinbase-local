const CoinbasePro = require('coinbase-pro');

export class Orderbook
{
  orderbookHandle:any;
  products:string[];

  constructor(products:string[])
  {
    this.products = products;
    console.log(`constructing orderbook for ${products}`);
  }

  async init() {
    let syncCount = 0;

    this.orderbookHandle = new CoinbasePro.OrderbookSync(this.products);

    let p = new Promise((accept:Function,reject:Function) => {
      this.orderbookHandle.on('sync',(product:string) => { syncCount++; });
      this.orderbookHandle.on('synced',(product:string) => { 
        if(--syncCount == 0) accept();
      });
      this.orderbookHandle.on('error',(error:string) => { reject(error); });
    });
    await p;
  };

  state(product:string) { return this.orderbookHandle.books[product].state(); }
}
