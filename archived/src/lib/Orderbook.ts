import { backOff } from "exponential-backoff";
const CoinbasePro = require('coinbase-pro');

export class Orderbook
{
  orderbookHandle:any;
  active:boolean;
  products:string[];


  protected async getHandle(products:string[]):Promise<any>
  {
    this.active = false;
    let orderbookHandle = new CoinbasePro.OrderbookSync(this.products);
    let syncCount = 0;

    let p = new Promise((accept:Function,reject:Function) => {
      orderbookHandle.on('synced',(product:string) => { 
        orderbookHandle.on('error',(err:string) => {
          if(this.active)
          {
            console.log("error encountered ${err}... resetting connection...");
            this.active = false;
            this.init();
          }
        });
        orderbookHandle.on('close',(err:string) => {
          if(this.active)
          {
            console.log("socket close ... resetting connection...");
            this.active = false;
            this.init();
          }
        });
        if(++syncCount == this.products.length)
        {
          this.active = true;
          accept();
        }
      });
      orderbookHandle.on('error',(err:string) => { 
        console.log("orderbook handle error",err);
        reject(err);
      });
    });
    await p;
    return orderbookHandle;
  }

  constructor(products:string[])
  {
    this.products = products;
    console.log(`constructing orderbook for ${products}`);
  }

  async init() {
    let startingDelay = 10000;
    let maxDelay = 120000;

    while(!this.active)
    {
      try
      { 
        this.orderbookHandle = await backOff(() => this.getHandle(this.products),{ startingDelay:startingDelay, maxDelay:maxDelay });
      }
      catch(err)
      { 
        console.log(`error during sync ${err} retrying...`);
        startingDelay = 120000;
      }
    }
  }

  circularBufferArray(product:string) { return this.orderbookHandle._circular_buffers[product].toarray() }
  state(product:string) { return this.orderbookHandle.books[product].state(); }
  sequence(product:string) { return this.orderbookHandle._sequences[product]; }
}
