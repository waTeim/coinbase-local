"use strict";

var copyfiles = require('copyfiles');

    
copyfiles(['src/bin/*','bin'],true,function (err) { if(err)console.error(err) });
