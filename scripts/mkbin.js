"use strict";

var mkdirp = require('mkdirp');
    
mkdirp('bin',{},function (err) { if(err)console.error(err) });
