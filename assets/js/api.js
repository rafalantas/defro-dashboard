/* Same-origin client for the dashboard API. */
(function(){
  const allowedRoot='/api/';
  function validatePath(path, method){
    if(typeof path!=='string'||!path.startsWith(allowedRoot)||path.startsWith('//')) throw new TypeError('Dozwolone są wyłącznie ścieżki /api/.');
  }
  async function get(path){
    validatePath(path,'GET');
    return fetch(path,{method:'GET',cache:'no-store',headers:{'Accept':'application/json'}});
  }
  async function post(path, body){
    validatePath(path,'POST');
    const headers={'Accept':'application/json'};
    const options={method:'POST',cache:'no-store',headers};
    if(body!==undefined){headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
    return fetch(path,options);
  }
  window.ThermoFlowApi=Object.freeze({get,post});
})();
