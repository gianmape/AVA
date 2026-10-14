// Patch IAS binding URL: replace accounts.cloud.sap with accounts.ondemand.com
const vcap = JSON.parse(process.env.VCAP_SERVICES || '{}');
if (vcap.identity) {
    vcap.identity.forEach(binding => {
        const c = binding.credentials;
        if (!c) return;
        ['url', 'authorization_endpoint', 'end_session_endpoint'].forEach(key => {
            if (c[key]) c[key] = c[key].replace('accounts.cloud.sap', 'accounts.ondemand.com');
        });
    });
    process.env.VCAP_SERVICES = JSON.stringify(vcap);
}

const approuter = require('@sap/approuter');
const ar = approuter();
ar.start();
