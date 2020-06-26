var D2UConverter = new require('dos2unix').dos2unix;

// Setup default options
var defaultOptions = {
  glob: {
    cwd: __dirname
  },
  maxConcurrency: 50
};

var d2u = new D2UConverter(defaultOptions)
  .on('error', function(err) {
    console.error(err);
  })
  .on('end', function(stats) {
    console.log(stats);
  });

// Convert line endings of a single non-binary, non-irregular file from
// '\r\n' to '\n'.
d2u.process(['bin/*']);
