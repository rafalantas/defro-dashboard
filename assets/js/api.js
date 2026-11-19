/* GET-only client for the existing dashboard API. */
(function(){
  const allowedRoot='/api/';
  async function get(path){
    if(typeof path!=='string'||!path.startsWith(allowedRoot)||path.startsWith('//')) throw new TypeError('Dozwolone są wyłącznie ścieżki GET /api/.');
    return fetch(path,{method:'GET',cache:'no-store',headers:{'Accept':'application/json'}});
  }
  window.ThermoFlowApi=Object.freeze({get});
})();
