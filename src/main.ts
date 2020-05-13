import init from './init';

let PORT:number;

if(process.env.PORT != null) PORT = parseInt(process.env.PORT);
else PORT = 63200;

async function initServer(products:string[])
{
  let app:any = await init(products);
  let server = app.listen(PORT, () => { console.log(`App is running at http://localhost:${PORT}`); });

  return server;
}

export default async function main(program:any)
{
  let products:string[] = program.args;

  if(products.length > 0) await initServer(products);
  else program.help();
}
