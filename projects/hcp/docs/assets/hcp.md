# HCP Management API Reference

## Introduction to the HCP management API

The Hitachi Content Platform (HCP) management API is a RESTful HTTP interface to a subset of the administrative functions of an HCP system. Using this API, you can manage tenants, namespaces, retention classes, content classes, and tenant-level user and group accounts (HCP tenants only).

Each entity you can manage is referred to as a resource. Each resource has properties that provide information about it.

Note:

- Most of the examples in this book use cURL and Python with PycURL, a Python interface that uses the libcurl library. cURL and PycURL are both freely available open-source software. You can download them from [http://curl.haxx.se](http://curl.haxx.se/).
- In version 7.12.1 of PycURL, the PUT method was deprecated and replaced with UPLOAD. The Python examples in this book show UPLOAD but work equally well with PUT.

### What you can do with the management API

The HCP management API lets you work with tenants, namespaces, retention classes, content classes, and tenant-level user and group accounts.

For tenants, you can:

- Modify HCP tenants
- Retrieve information about a single tenant
- Set default properties for namespaces created for an HCP tenant
- Retrieve statistics about the content of the namespaces owned by a tenant
- Generate a chargeback report for an HCP tenant

For namespaces, you can:

- Create HCP namespaces
- Modify namespaces
- Delete HCP namespaces
- Retrieve information about a single namespace
- Retrieve a list of all the namespaces owned by a tenant
- Retrieve statistics about the content of a namespace
- Generate a chargeback report for an HCP namespace
- Reset the indexing checkpoint for a namespace

For retention classes, you can:

- Create, modify, and delete retention classes for a namespace
- Retrieve information about a single retention class
- Retrieve a list of all the retention classes defined for a namespace

For content classes, you can:

- Create, modify, and delete content classes for a tenant
- Retrieve information about a single content class
- Retrieve a list of all the content classes defined for a tenant

For HCP tenant-level user accounts, you can:

- Create, modify, and delete user accounts
- Retrieve information about a single user account
- Retrieve a list of all the user accounts defined for a tenant

For HCP tenant-level group accounts, you can:

- Create, modify, and delete group accounts
- Retrieve information about a single group account
- Retrieve a list of all the group accounts defined for a tenant
- Create a new group account with the security role or give the security role to an existing group account

### Who can use the management API

To use the HCP management API, you need either a system-level or tenant-level user account that’s defined in HCP. If HCP is configured to support Windows® Active Directory® (AD), clients can also use recognized AD user accounts to access HCP through the management API. A recognized AD user account is an AD user account for a user that belongs to one or more AD groups for which corresponding group accounts are defined in HCP.

What you can do with the API depends on:

- The level of account you’re using
- The roles associated with the account (or applicable group accounts)
- For tenant-level accounts, whether the account (or applicable group accounts) has the allow namespace management property

The permissions granted by each role have the same effect with the management API as they do in the Tenant Management Console . For example, with an HCP tenant-level user account that includes the administrator role, you can create, modify, and delete namespaces. With a tenant-level user account that includes only the monitor role, you can only retrieve information about these entities.

An HCP tenant can grant system-level users administrative access to itself. This enables users with system-level user accounts to perform the activities allowed by the tenant-level roles that correspond to their system-level roles.

If you have only the allow namespace management property and no roles, the activities you can perform with the HCP management API are limited to creating namespaces, listing and deleting namespaces you own, and viewing and modifying the versioning status of namespaces you own.

For you to use the management API with a system-level user account, the API must be enabled at the system level. For you to use the management API with a tenant-level user account, the API must be enabled at both the system and tenant levels.

### Resources and properties

Each entity that you can manage independently in the HCP management API is called a resource. Examples of resources are tenants and namespaces.

Resources have properties. The values of these properties describe the resource. For example, tenant properties include the tenant name, description, and whether system-level users can manage the tenant.

Some properties are treated as resources in their own right. For example, the Tenant Management Console configuration is a property of a tenant, but it is treated as a resource.

To identify a resource, you use a URL. For example, this URL identifies the tenant resource named Finance in the HCP system named hcp.example.com:

```
https://finance.hcp.example.com:9090/mapi/tenants/finance
```

You also use URLs to identify lists of resources. For example, this URL identifies the list of namespaces owned by the Finance tenant:

```
https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces
```

Each URL that identifies a resource or list of resources has a data type. For a list of resources, the data type is list. For an individual resource, the data type is a named unordered set of properties. For example, the data type for the retention class resource is retentionClass. The properties included in this data type are name, value, description, and allowDisposition.

Properties also have data types. The data type of a property can be string, integer, long, Boolean, or list, or it can be another named unordered set of properties. For example, the name property for a tenant resource has a data type of string. The ipSettings property for a Tenant Management Console configuration resource has a data type of ipSettings.

### Supported methods

The HCP management API supports the HTTP methods listed in the list below.

PUT
Creates a resource.When creating a resource, you need to supply values for all properties that do not have default values. If properties with default values are omitted from the request body, those values are applied to the resource.GET
Retrieves information about an individual resource or retrieves a list of resources of a given type.HEAD
Performs a GET but does not return the response body. You can use a HEAD request to check whether a particular resource exists.POST
Modifies a resource.When modifying a resource, you need to supply values only for the properties whose values you want to change. If properties are omitted from the request body, they remain unchanged.DELETE
Deletes a resource.OPTIONS
Describes the methods supported by a given resource.

Each request you submit to the management API can work on only one resource. So, for example, you cannot use a single PUT request to create two tenants.

### Input and output formats

When you create or modify a resource through the HCP management API, you use XML or JSON to specify the resource properties. When you request information about resources, you can ask for the response to be returned in XML format or in JSON format. For one resource, chargebackReport, you can also ask for the response to be returned in CSV format.

The response to an OPTIONS request is always returned as Web Application Description Language (WADL). WADL is an XML-based description language for HTTP-based web applications.

All responses returned through the management API are UTF-8 encoded. The request bodies you create for input to the API must also be UTF-8 encoded.

## HTTP Content-Type and Accept headers

With a PUT or POST request, you use the HTTP `Content-Type` request header to specify the format of the request body. This header is required if the request includes a request body.

With a GET request, you can use the HTTP `Accept request` header to specify the format for the response body. If you omit this header, the API returns the response body in XML format.

In a `Content-Type` or `Accept` header, you specify the input or output format as an Internet media type:

- For XML, the Internet media type is application/xml.
- For JSON, the Internet media type is application/json.
- For JSON with callback, the Internet media type is application/javascript.
- For CSV, the Internet media type is text/csv.

You don’t need to specify an Internet media type in an OPTIONS request. If you do specify one, it is ignored.

With cURL, you use the -H option to specify an HTTP header. So, for example, to specify that a request body uses XML, you include this in the curl command:

```
-H "Content-Type: application/xml"
```

In Python with PycURL, you do this with the HTTPHEADER option. For example:

```
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml"])
```

HTTP headers and Internet media types are not case sensitive.

## XML

In an XML request or response body:

- Property names are element names. Property values are element values. For example, the element that corresponds to the softQuota property with a value of 85 is:


```
<softQuota>85</softQuota>
```

- The name of the root element for a request that involves a single resource is the data type of that resource. For example, for this URL, which identifies a single namespace named Accounts-Receivable, the root element is namespace:


```
https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces/Accounts-Receivable
```

- The name of the root element for a request for a list of resources is the term used to identify those resources in the URL. For example, for this URL, the root element is namespaces:


```
https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces
```

- In a list of resources, each resource is the value of an element whose name is the name of the property used to identify the resource. For example, the response body for a request for the namespaces owned by the Finance tenant might include this:


```
<namespaces>
      <name>Accounts-Payable</name>
      <name>Accounts-Receivable</name>
</namespaces>
```


Here’s a request for complete information about the Accounts-Receivable namespace to be returned in XML format:

```
curl -k -i -H "Accept: application/xml"
    -H "Authorization: bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces/
        accounts-receivable?verbose=true&prettyprint"
```

Here’s the XML response body you get when you make the request using a user account that includes the administrator role:

```
<namespace>
    <aclsUsage>ENFORCED</aclsUsage>
    <authUsersAlwaysGrantedAllPermissions>true
    </authUsersAlwaysGrantedAllPermissions>
    <allowPermissionAndOwnershipChanges>true
    </allowPermissionAndOwnershipChanges>
    <appendEnabled>false</appendEnabled>
    <atimeSynchronizationEnabled>false</atimeSynchronizationEnabled>
    <authMinimumPermissions>
        <permission>BROWSE</permission>
        <permission>READ</permission>
        <permission>WRITE</permission>
    </authMinimumPermissions>
    <creationTime>2017-02-09T15:42:36-0500</creationTime>
    <customMetadataIndexingEnabled>true</customMetadataIndexingEnabled>
    <customMetadataValidationEnabled>true</customMetadataValidationEnabled>
    <description>Created for the Finance department at Example Company by Lee
        Green on 2/9/2017.</description>
    <dpl>Dynamic</dpl>
    <enterpriseMode>true</enterpriseMode>
    <allowErasureCoding>true</allowErasureCoding>
    <fullyQualifiedName>Accounts-Receivable.Finance.hcp.example.com
    </fullyQualifiedName>
    <hardQuota>50 GB</hardQuota>
    <hashScheme>SHA-256</hashScheme>
    <indexingDefault>true</indexingDefault>
    <indexingEnabled>true</indexingEnabled>
    <isDplDynamic>true</isDplDynamic>
    <mqeIndexingTimestamp>2017-02-26T18:11:13-0400</mqeIndexingTimestamp>
    <multipartUploadAutoAbortDays>30</multipartUploadAutoAbortDays>
    <name>Accounts-Receivable</name>
    <optimizedFor>CLOUD</optimizedFor>
    <owner>pblack</owner>
    <ownerType>LOCAL</ownerType>
    <readFromReplica>true</readFromReplica>
    <replicationEnabled>true</replicationEnabled>
    <replicationTimestamp>2017-02-27T06:45:52-0500</replicationTimestamp>
    <searchEnabled>true</searchEnabled>
    <servicePlan>Short-Term-Activity</servicePlan>
    <serviceRemoteSystemRequests>true</serviceRemoteSystemRequests>
    <softQuota>75</softQuota>
    <tags>
        <tag>Billing</tag>
        <tag>lgreen</tag>
    </tags>
    <id>0e774b8d-8936-4df4-a352-b68766b5c287</id>
    <authAndAnonymousMinimumPermissions>
        <permission>BROWSE</permission>
        <permission>READ</permission>
    </authAndAnonymousMinimumPermissions>
</namespace>
```

## JSON

In a JSON request or response body:

- Properties are name/value pairs. For example, the name/value pair that corresponds to the softQuota property with a value of 85 is:


```
"softQuota":"85"
```

- A list of resources is represented by a name/value pair, where the name is the name of the property used to identify each resource and the value is a comma-separated list of the resource identifiers. For example, the response body for a request to list the namespaces owned by the Finance tenant might look like this:


```
{
      "name" : [ "Accounts-Payable", "Accounts-Receivable ]
}
```


Here’s a request for complete information about the Accounts-Receivable namespace to be returned in JSON format:

```
curl -k -i -H "Accept: application/json"
    -H "Authorization: bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces/
        accounts-receivable?verbose=true&prettyprint"
```

Here’s the JSON response body you get when you make the request using a user account that includes the administrator role:

```
{
    "aclsUsage" : "ENFORCED",
    "authUsersAlwaysGrantedAllPermissions" : :true,
    "allowPermissionAndOwnershipChanges" : true,
    "appendEnabled" : false,
    "atimeSynchronizationEnabled" : false,
    "authMinimumPermissions" : {
        "permission" : [ "BROWSE", "READ", "WRITE" ]
    },
    "creationTime" : "2017-02-09T15:42:36-0500",
    "customMetadataIndexingEnabled" : true,
    "customMetadataValidationEnabled" : true,
    "description" : "Created for the Finance department at Example Company by Lee
        Green on 2/9/2017.",
    "dpl" : "Dynamic",
    "enterpriseMode" : true,
    "allowErasureCoding" : true,
    "fullyQualifiedName" : "Accounts-Receivable.Finance.hcp.example.com",
    "hardQuota" : "50 GB",
    "hashScheme" : "SHA-256",
    "indexingDefault" : true,
    "indexingEnabled" : true,
    "isDplDynamic" : true,
    "mqeIndexingTimestamp" : "2017-02-26T18:11:13-0400",
    "multipartUploadAutoAbortDays" : 30,
    "name" : "Accounts-Receivable",
    "optimizedFor" : "CLOUD",
    "owner" : "pblack",
    "ownerType" : "LOCAL",
    "readFromReplica" : true,
    "replicationEnabled" : true,
    "replicationTimestamp" : "2017-02-27T06:45:52-0500",
    "searchEnabled" : true,
    "servicePlan" : "Short-Term-Activity",
    "serviceRemoteSystemRequests" : true,
    "softQuota" : 75,
    "tags" :
        "tag" : [ "Billing", "lgreen" ]
    },
    "id" : "0e774b8d-8936-4df4-a352-b68766b5c287",
    "authAndAnonymousMinimumPermissions" : {
        "permission" : [ "BROWSE", "READ" ]
    }
}
```

## CSV

In a CSV response body (only for a GET of a chargebackReport resource), the name of each reported property for the resource is a field in the first line. Property values are fields in the subsequent lines.

Here’s a request for the chargebackReport resource for the Accounts-Receivable namespace to be returned in CSV format:

```
curl -k -i -H "Accept: text/csv"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces/
         accounts-receivable/chargebackReport?start=2017-02-18T13:00:00-0500
         &end=2017-02-18T13:59:59-0500&granularity=hour"
```

Here’s the CSV response body:

```
ystemName,tenantName,namespaceName,startTime,endTime,objectCount,
    ingestedVolume,storageCapacityUsed,bytesIn,bytesOut,reads,writes,deletes,
    multipartObjects,multipartObjectParts,multipartObjectBytes,multipartUploads,
    multipartUploadParts,multipartUploadBytes,deleted,valid
hcp.example.com,Finance,Accounts-Receivable,2017-02-18T13:00:00-0500,1240,
    2017-02-18T13:59:59-0500,6,134243721,134270976,134243721,87561,1,11,0,2,
    7,93213889,0,0,0,false,true
```

## WADL

The response body for an OPTIONS request is always returned as WADL. The HTTP response headers include Allow, which lists the supported methods for the resource.

Here’s a request for the methods you can use with the user accounts resource:

```
curl -k -iX OPTIONS
    -H "Authorization: bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp.example.com:9090/mapi/tenants/finance/userAccounts
         ?prettyprint"
```

Here are the response headers:

```
HTTP/1.1 200 OK
Content-Type: application/vnd.sun.wadl+xml
Allow: OPTIONS,HEAD,POST,GET,PUT
X-HCP-SoftwareVersion: 6.0.1.64
Content-Length: 3575
```

Here’s the WADL response body:

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<application xmlns="http://research.sun.com/wadl/2006/10">
    <doc xmlns:jersey="http://jersey.dev.java.net/"
        jersey:generatedBy="Jersey: 1.1.5 01/20/2010 04:04 PM"/>
    <resources base="https://admin.hcp.example.com:9090/mapi/">
        <resource path="tenants/finance/userAccounts">
            <method name="PUT" id="createUserAccount">
                <request>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="password"/>
                    <representation mediaType="application/xml"/>
                    <representation mediaType="application/json"/>
                </request>
                <response>
                    <representation mediaType="*/*"/>
                </response>
            </method>
            <method name="HEAD" id="getUserAccountsHead">
                <request>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="offset"/>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="count"/>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="filterType"/>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="filterString"/>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="sortType"/>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="sortOrder"/>
                </request>
                <response>
                    <representation mediaType="application/xml"/>
                    <representation mediaType="application/json"/>
                    <representation mediaType="application/javascript"/>
                </response>
            </method>
            <method name="GET" id="getUserAccounts">
                <request>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="offset"/>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="count"/>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="filterType"/>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="filterString"/>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="sortType"/>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="sortOrder"/>
                </request>
                <response>
                    <representation mediaType="application/xml"/>
                    <representation mediaType="application/json"/>
                    <representation mediaType="application/javascript"/>
                </response>
            </method>
            <method name="POST" id="resetPasswords">
                <request>
                    <param xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        type="xs:string" style="query" name="resetPasswords"/>
                </request>
                <response>
                    <representation mediaType="application/xml"/>
                    <representation mediaType="application/json"/>
                </response>
            </method>
        </resource>
    </resources>
</application>
```

### Query parameters

Some HCP management API requests take query parameters. Query parameters are appended to a resource URL following a question mark (?). Multiple parameters are joined by ampersands (&).

The following considerations apply to query parameters:

- If you specify an invalid value for a query parameter that requires a Boolean value (true or false), HCP interprets the value as false.
- If you specify an invalid value for any other required or optional query parameter, HCP returns a status code of 400 (Bad Request).
- If you omit a required query parameter, HCP returns a status code of 400 (Bad Request).
- If you specify a query parameter that’s not valid for the request, HCP ignores it.

Query parameter names are case sensitive.

## verbose

The `verbose` query parameter tells HCP how much information to return in response to a `GET` request for information about a resource. Valid values for this parameter are `true` and `false`.

In most cases, with `verbose=false`, a request for information about a resource returns only the properties whose values you can modify. For example, you cannot change the type of authentication for a user account. So, when you use `GET` with `verbose=false` to retrieve information about a user account, the localAuthentication property is omitted from the response body.

To retrieve all the properties for a resource, you need to append `verbose=true` to the resource URL. If you omit the `verbose` parameter, HCP uses the default value `false`.

## prettyprint

The `prettyprint` query parameter causes the XML or JSON returned in response to a GET request to be formatted for readability. For example, with the prettyprint parameter, the returned XML for a list of namespaces looks like this:

```
<namespaces>
    <name>Accounts-Payable</name>
    <name>Accounts-Receivable</name>
</namespaces>
```

Without the `prettyprint` parameter, the returned XML looks like this:

```
<?xml version="1.0" encoding="UTF-8"?><namespaces><name>Accounts-Payable</name><name>Accounts-Receivable</name></namespaces>
```

The `prettyprint` parameter increases the time it takes to process a request. Therefore, you should use it only for testing purposes and not in production applications.

## Request-specific query parameters

Some requests take query parameters that provide additional information to HCP about the operation you want to perform or that request a particular operation.

When the only action you’re requesting in a POST request for a resource other than a replication resource is specified by a query parameter, you need to provide an empty request body. With cURL, you specify this body as the argument for the -d option in the request:

With a content type of XML, the argument is an empty root element for the resource being modified, enclosed in double quotation marks, like this:

```
-d "<root-element/>"
```

For example, here’s a request to change only the password for the user account with the username mwhite:

```
curl -k -i -d "<userAccount/>" -H "Content-Type: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/
         userAccounts/mwhite?password=p4ssw0rd"
```

With a content type of JSON, the argument is an empty pair of curly braces enclosed in double quotation marks, like this:

```
-d "{}"
```

### HCP product-specific response headers

For an HCP management API request, the HTTP response headers always include the HCP-specific `X-HCP-SoftwareVersion` header. The value of this header is the version number of the currently installed HCP software; for example:

```
X-HCP-SoftwareVersion: 9.0.0.2
```

If a management API request results in an error and additional information about the error is available, the HTTP response headers include the HCP-specific `X-HCP-ErrorMessage` header; for example:

```
X-HCP-ErrorMessage: 'password' parameter is required.
```

### Security-related response headers

For an HCP management API request, the HTTP response headers always include headers that address browser security concerns. These headers have fixed values. The table below describes these headers.

| Header | Value | Description |
| --- | --- | --- |
| Cache-Control | no-cache,no-store,must- revalidate | Specifies directives that must be obeyed by all caching mechanisms along the request/response chain |
| Content-Security- Policy | default-src 'self'; script-src 'self' 'unsafe-eval' 'unsafe- inline'; connect-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'self'; frame-ancestors 'self'; | Restricts the content that the browser can load to the sources specified by the header value |
| Expires | Thu, 01 Jan 1970 00:00:00 GMT | Causes the response to become stale immediately after it is sent |
| Pragma | no-cache | Prevents the response from being used for subsequent requests for the same resource without the browser first checking whether the resource has changed |
| X-Content-Type- Options | nosniff | Prevents the browser from examining the returned content to determine the content MIME type |
| X-DNS-Prefetch- Control | off | Prevents the browser from performing domain name resolution on URLs embedded in returned content before the URLs are requested |
| X-Download- Options | noopen | Prevents the browser from opening resources that are downloaded through links in the returned content |
| X-Frame-Options | SAMEORIGIN | Prevents the browser from rendering the returned content in a frame on a page containing content not returned by the HCP system |
| X-XSS-Protection | 1; mode=block | Stops the browser from loading the returned content if the browser detects reflected cross-site scripting (XSS) in the response |

Note: The Cache-Control and Expires headers are not returned with error responses.


### Enabling the management API in the Consoles

1. Log into the System Management Console or Tenant Management Console using a user account with the security role.
2. In the top-level menu of either Console, select Security \> MAPI.
3. In the Management API Setting section on the Management API page, select Enable the HCP management API.
4. Click Update Settings.

### Support for the Amazon S3 API

HCP is compatible with the Amazon Simple Storage Service (Amazon S3) REST API, which allows clients to store objects in namespaces. A namespace is a container for objects that has its own settings, such as ownership and lifecycle. Using HCP, users can perform common operations on objects and namespaces.

For information about using Amazon S3, see the [Amazon S3 API documentation](https://docs.aws.amazon.com/AmazonS3/latest/dev/Welcome.html).

The following tables list the level of support for each of the HCP S3 API methods compared with the Amazon S3 API methods and describes any implementation differences in the S3 APIs.

| Amazon S3 API | Support level | Implementation differences |
| --- | --- | --- |
| Authenticating Requests (AWS Signature Version 4) | Supported | None |
| Authenticating Requests (AWS Signature Version 2) | Supported | None |
| GET Service / List Buckets | Supported | None |
| DELETE Bucket | Supported | None |
| DELETE Bucket analytics | Not supported | None |
| DELETE Bucket cors | Supported | None |
| DELETE Bucket encryption | Not supported | None |
| DELETE Bucket inventory | Not supported | None |
| DELETE Bucket lifecycle | Not supported | None |
| DELETE Bucket metrics | Not supported | None |
| DELETE Bucket policy | Not supported | None |
| DELETE Bucket replication | Not supported | None |
| DELETE Bucket tagging | Not supported | None |
| DELETE Bucket website | Not supported | None |
| GET Bucket (List Objects) Version 1 | Supported | None |
| GET Bucket (List Objects) Version 2 | Supported | None |
| GET Bucket accelerate | Not supported | None |
| GET Bucket acl | Supported | None |
| GET Bucket analytics | Not supported | None |
| GET Bucket cors | Supported | None |
| GET Bucket encryption | Not supported | None |
| GET Bucket inventory | Not supported | None |
| GET Bucket lifecycle | Not supported | None |
| GET Bucket location | Not supported | None |
| GET Bucket logging | Not supported | None |
| GET Bucket metrics | Not supported | None |
| GET Bucket notification | Not supported | None |
| GET Bucket Object versions | Supported | None |
| GET Bucket policy | Not supported | None |
| GET Bucket replication | Not supported | None |
| GET Bucket requestPayment | Not supported | None |
| GET Bucket tagging | Not supported | None |
| GET Bucket versioning | Supported | None |
| GET Bucket website | Not supported | None |
| HEAD Bucket | Supported | None |
| List Bucket Analytics Configurations | Not supported | None |
| List Bucket Inventory Configurations | Not supported | None |
| List Bucket Metrics Configurations | Not supported | None |
| List Multipart Uploads | Supported | None |
| PUT Bucket | Supported | None |
| PUT Bucket accelerate | Not supported | None |
| PUT Bucket acl | Supported | None |
| PUT Bucket analytics | Not supported | None |
| PUT Bucket cors | Supported | None |
| PUT Bucket encryption | Not supported | None |
| PUT Bucket inventory | Not supported | None |
| PUT Bucket lifecycle | Not supported | None |
| PUT Bucket logging | Not supported | None |
| PUT Bucket metrics | Not supported | None |
| PUT Bucket notification | Not supported | None |
| PUT Bucket policy | Not supported | None |
| PUT Bucket replication | Not supported | None |
| PUT Bucket requestPayment | Not supported | None |
| PUT Bucket tagging | Not supported | None |
| PUT Bucket versioning | Supported | None |
| PUT Bucket website | Not supported | None |
| Delete Multiple Objects | Supported | None |
| DELETE Object (delete object version not supported) | Supported | None |
| DELETE Object tagging | Not supported | None |
| GET Object | Supported | None |
| GET Object ACL | Supported | None |
| GET Object tagging | Not supported | None |
| GET Object torrent | Not supported | None |
| HEAD Object | Supported | None |
| OPTIONS object | Supported | None |
| POST Object | Supported | None |
| POST Object restore | Not supported | None |
| PUT Object | Supported | None |
| PUT Object - Copy | Supported | None |
| PUT Object acl | Supported | None |
| PUT Object tagging | Not supported | None |
| SELECT Object Content | Not supported | None |
| Abort Multipart Upload | Supported | None |
| Complete Multipart Upload | Supported | None |
| Initiate Multipart Upload | Supported | None |
| List Parts | Supported | None |
| Upload Part | Supported | None |
| Upload Part - Copy | Supported | None |
| Pre-signed URLs | Supported | None |
| Server-side Encryption | Supported | None |
| Client-side Encryption | Not supported | None |
| HCP Custom Retention header<br>X-HCP-RETENTION | Supported | None |
| HCP Custom privilege delete header<br>X-HCP-PRIVILEGED | Supported | None |
| HCP Custom Retention Hold header<br>X-HCP-RETENTIONHOLD | Supported | None |
| HCP Custom multiple Labeled Retention Hold<br>X-HCP-LABELRETENTIONHOLD | Supported | None |
| Addressing Virtual host | Supported | None |
| Addressing Path style | Supported | None |
| Signed/Unsigned payload | Supported | None |
| Chunked request | Supported | None |
| DeletePublicAccessBlock | Not supported | None |
| GetBucketPolicyStatus | Not supported | None |
| GetObject: PartNumber | Not supported | None |
| GetPublicAccessBlock | Not supported | None |
| PutObjectLegalHold | Supported | None |
| PutObjectLockConfiguration | Supported | None |
| PutObjectRetention | Supported | None |
| PutPublicAccessBlock | Not supported | None |

## Access and authentication

With the HCP management API, resources are represented by URLs. Each request you make must specify one such URL. Each request must also include the credentials for the account you’re using to access HCP through the management API.

This section of the Help describes resource URLs and explains how to include account credentials in a management API request.

### URL for HCP access through the management API

#### Using an IP address in URL

Normally, you let HCP choose the node on which to process a management API request. You can, however, use an IP address in the URL to access the system on a specific node. To do this, you replace the fully qualified domain name (FQDN) in the URL with the IP address for the node you want.

If the node has both an IPv4 address and an IPv6 address, you can use either address. For example, to access the namespaces resource on a node that has both the IPv4 address 192.168.210.16 and the IPv6 address 2001:0db8::101, you can use either of these URLs:

```
https://192.168.210.16:9090/mapi/tenants/finance/namespaces
https://[2001:0db8::101]:9090/mapi/tenants/finance/namespaces
```

Additionally, you need to provide the fully qualified domain name of the tenant in an HTTP Host header. With cURL, you do this with the `-H` option. For example:

```
-H "Host: finance.hcp.example.com"
```

In Python with PycURL, you do this with the HTTPHEADER option. For example:

```
curl.setopt(pycurl.HTTPHEADER, ["Host: finance.hcp.example.com"])
```

Note: If you don’t know the IP addresses for the nodes in the HCP system, contact your HCP system administrator.


#### Using a hosts file

Typically, HCP is included as a subdomain in your DNS. If this is not the case, for access to the system, you need to use the tenant domain name in the URL and use a hosts file to define mappings of one or more node IP addresses to that domain name.

The location of the hosts file depends on the client operating system:

- On Windows, by default: c:\\windows\\system32\\drivers\\etc\\hosts
- On Unix: /etc/hosts
- On Mac OS® X: /private/etc/hosts

Note: If HCP is not included in your DNS, the tenant domain name is a dummy domain name that follows the conventions for well-formed DNS names.


## Hostname mappings

Each entry in a hosts file is a mapping of an IP address to a hostname. For an HCP tenant, the hostname must be the fully qualified domain name (FQDN) for the tenant.

Each hosts file entry you create for access to a tenant must include:

- An IP address of a node in the HCP system
- The FQDN of the tenant domain

For example, if the tenant domain name is finance.hcp.example.com and one of the HCP nodes has the IPv4 address 192.168.210.16 and the IPv6 address 2001:0db8::101, you could add either or both of these lines to the hosts file on the client:

```
192.168.210.16   finance.hcp.example.com
2001:0db8::101   finance.hcp.example.com
```

You can include comments in a hosts file either on separate lines or following a mapping on the same line. Each comment must start with a number sign (#). Blank lines are ignored.

## Hostname mapping considerations

In the hosts file, you can map IP addresses for any number of nodes to a single domain name. The way a client uses multiple IP address mappings for a single domain name depends on the client platform. For information about how your client handles hosts file entries that define multiple IP address mappings for a single domain name, see your client documentation.

If any of the HCP nodes listed in the hosts file are unavailable, timeouts may occur when you use a hosts file to access the system through the management API.

## Sample hosts file

Here’s a sample hosts file that contains mappings for the Finance tenant for nodes with both IPv4 and IPv6 addresses:

```
192.168.210.16   finance.hcp.example.com
192.168.210.17   finance.hcp.example.com
192.168.210.18   finance.hcp.example.com
192.168.210.19   finance.hcp.example.com
2001:0db8::101   finance.hcp.example.com
2001:0db8::102   finance.hcp.example.com
2001:0db8::103   finance.hcp.example.com
2001:0db8::104   finance.hcp.example.com
```

#### URL considerations

The following considerations apply to URLs in management API requests.

## URL length

The portion of a URL that follows mapi, excluding any appended query parameters, is limited to 4,095 bytes. If a request includes a URL that violates that limit, HCP returns a status code of 414 (Request URI Too Large).

## Percent-encoding for special characters

Some characters have special meaning when used in a URL and may be interpreted incorrectly when used for other purposes. To avoid ambiguity, percent-encode the special characters listed in the table below.

Percent-encoded values are not case sensitive.

| Character | Percent-encoded values |
| --- | --- |
| Space | %20 |
| Tab | %09 |
| New line | %0A |
| Carriage return | %0D |
| + | %2B |
| % | %25 |
| # | %23 |
| ? | %3F |
| & | %26 |
| \ | &5C |

## Quotation marks with URLs in command lines

When using the HCP management API, you work in a Windows, Unix, or Mac OS X shell. Some characters in the commands you enter may have special meaning to the shell. For example, the ampersand (&) used in URLs to join multiple query parameters may indicate that a process should be put in the background.

To avoid the possibility of the Windows, Unix, or Mac OS X shell misinterpreting special characters in a URL, always enclose the entire URL in double quotation marks.

### Authentication

To use the HCP management API, you need either a system-level or tenant-level user account that’s defined in HCP. If HCP is configured to support Windows Active Directory® (AD), you can also use an AD user account that HCP recognizes to access HCP through the metadata query API.

HCP also accepts Active Directory authentication provided through the SPNEGO protocol or the AD authentication header. For more information about SPNEGO, see [http://tools.ietf.org/html/rfc4559](http://tools.ietf.org/html/rfc4559).

Note: To use a user account that was created in an HCP release earlier than 4.0, you need to log into the System Management Console or Tenant Management Console, as applicable, with that account at least once before you can use the account with the management API.


You need to provide credentials with every management API request. If you do not provide credentials or provide invalid credentials, HCP responds with a 403 (Forbidden) error message.

To provide credentials in a management API request, you specify an `authentication token` in an HTTP Authorization request header.

HCP also accepts credentials provided in an `hcp-api-auth` cookie. However, this method of providing credentials has been deprecated and should not be used in new applications.

#### HCP authentication through the management API

To authenticate with HCP through the management API, you need to construct an authentication token from a system-level user account or a tenant-level user account and then submit it using a request header with all requests. Successful authentication requires encoding your account information.

##### Authentication token

An authentication token consists of a username in Base64-encoded format and a password that’s hashed using the MD5 hash algorithm, separated by a colon, like this:

```
base64-encoded-user-name:md5-hashed-password
```

For example, here’s the token for the Base64-encoded user name lgreen and the MD5-hashed password p4ssw0rd:

```
bGdyZWVu:2a9d119df47ff993b662a8ef36f9ea20
```

The GNU Core Utilities include the base64 and md5sum commands necessary to encode your account information. These commands convert text to Base64-encoded and MD5-hashed values, respectively. For more information about the GNU Core Utilities, see [http://www.gnu.org/software/coreutils/](http://www.gnu.org/software/coreutils/).

Other tools that generate Base64-encoded and MD5-hashed values are available for download on the web. For security reasons, do not use interactive public web-based tools to generate these values.

The GNU Core Utilities include the base64 and md5sum commands, which convert text to Base64-encoded and MD5-hashed values, respectively. With these commands, a line such as this creates the required token:

```
echo `echo -n username | base64`:`echo -n password | md5sum` | awk
  '{print $1}'
```

The character before echo, before and after the colon, and following md5sum is a backtick (or grave accent). The -n option in the echo command prevents the command from appending a new line to the output. This is required to ensure correct Base64 and MD5 values.

##### Authorization header

You use the HTTP Authorization request header to provide the authentication token for an HCP management API request. The value of this header is `HCP` followed by the authentication token, in this format:

```
Authorization: HCP authentication-token
```

For example, here’s the Authorization header for a user named lgreen and password p4ssw0rd:

```
Authorization: HCP bGdyZWVu:2a9d119df47ff993b662a8ef36f9ea20
```

## Specifying the Authorization header with cURL

With cURL, you use the -H option to specify a header. So, for example, a request to list the tenants for the HCP system named hcp.example.com might look like this:

```
curl -k -i -H "Authorization: HCP bGdyZWVu:2a9d119df47ff993b662a8ef36f9ea20"
    -H "Accept: application/xml"
    "https://admin.hcp.example.com:9090/mapi/tenants"
```

## Specifying the authentication header in Python with PycURL

In Python with PycURL, you use the HTTPHEADER option to specify a header, as in this example:

```
curl.setopt(pycurl.HTTPHEADER, ["Authorization: HCP\
    bGdyZWVu:2a9d119df47ff993b662a8ef36f9ea20"])
```

#### Active Directory user authentication through the HCP management API

To authenticate to HCP with Active Directory, you need to construct an authentication token from a AD user account and then submit it using a request header with all requests. The username and password do not need to be encoded.

##### Active Directory authentication token

An AD authentication token consists of an AD username and password separated by a colon, like this:

```
AD-username:AD-password
```

For example, here’s the token for the username lgreen and the password p4ssw0rd:

```
lgreen@example.com:p4sswOrd
```

##### Active Directory authorization header

You use the HTTP Authorization request header to provide the authentication token for an AD user accessing HCP through the management API. The value of this header is AD followed by the authentication token, in this format:

```
Authorization: AD authentication-token
```

For example, here’s the Authorization header for a user named lgreen and password p4ssw0rd:

```
Authorization: AD lgreen@example.com:p4sswOrd
```

## Specifying the Authorization header with cURL

With cURL, you use the -H option to specify a header. So, for example, a request to list the tenants for the HCP system named hcp.example.com might look like this:

```
curl -i -k -H "Authorization: AD lgreen@example.com:p4sswOrd"
    "Accept: application/xml" "https://admin.hcp.example.com:9090/mapi/tenants"
```

## Specifying the authentication header in Python with PycURL

Tip: After you log in using the authorization AD header, the response header replies with an hcap `Set-Cookie` value. You can use this cookie for subsequent authentication requests instead of using the authoriation AD header. For example:


```
curl -ik  -b HCAP-Login="acmepKMUfWLEu"
"https://tenant.hcp.example.com:9090/mapi/tenants"
```

In Python with PycURL, you use the HTTPHEADER option to specify a header, as in this example:

```
curl.setopt(pycurl.HTTPHEADER, ["Authorization: AD\
    lgreen@example.com:p4sswOrd"])
```

## Resources

The main types of HCP management API resources are tenants, namespaces, retention classes, content classes, tenant-level user accounts, and tenant-level group accounts.

Each main type of resource is associated with a set of resource identifiers, each of which identifies one of these:

- A list of resources of that type
- An instance of that type of resource
- A property of that type of resource that is treated as a resource in its own right.

A resource identifier is the portion of a resource URL that follows `mapi`.

Examples of using the management API to manipulate resources are also included. All examples assume an HCP system that supports Active Directory.

Additionally, this section contains instructions for paging through, sorting, and filtering the results of GET requests for namespaces, user accounts, and data access permissions.

### Content class resources

Content class resources let you create, retrieve information about, modify, and delete content classes. These resources are not available for tenants that do not have search configuration enabled.

The tables below provide information about content class resources.

## .../tenants/tenant-name/contentClasses

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| contentClass | PUT | Create a content class for a tenant | - For an HCP tenant, tenant-level user account with the administrator role<br>- For the default tenant, system-level user account with the administrator role |  |
| List | GET | Retrieve a list of the content classes for a tenant | - For an HCP tenant, tenant-level user account with the monitor or administrator role<br>- For the default tenant, system-level user account with the monitor or administrator role | The listed content classes are identified by content class name.<br>In XML, the element that identifies each content class is `name`. The root element for the list of content classes is `contentClasses`.<br>In JSON, the name in the name/value pair that lists the content classes is `name`. |

## .../tenants/tenant-name/contentClasses/content-class-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| contentClass | GET | Retrieve information about a content class | - For an HCP tenant, tenant-level user account with the monitor or administrator role<br>- For the default tenant, system-level user account with the monitor or administrator role |  |
| N/A | HEAD | Check for the existence of a content class | - For an HCP tenant, tenant-level user account with the monitor or administrator role<br>- For the default tenant, system-level user account with the monitor or administrator role |  |
| contentClass | POST | Modify a content class | - For an HCP tenant, tenant-level user account with the administrator role<br>- For the default tenant, system-level user account with the administrator role |  |
| N/A | DELETE | Delete a content class | - For an HCP tenant, tenant-level user account with the administrator role<br>- For the default tenant, system-level user account with the administrator role | The content class cannot contain any content properties. |

#### Example: Creating a content class

Here’s a sample PUT request that creates a content class named DICOM and associates it with the Medical-Records namespace, which is search enabled. The content class is defined in an XML file named dicom.xml. The request is made using a tenant-level user account that includes the administrator role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<contentClass>
    <name>DICOM</name>
    <contentProperties>
        <contentProperty>
            <name>Doctor_Name</name>
            <expression>/dicom_image/doctor/name</expression>
            <type>STRING</type>
            <multivalued>false</multivalued>
            <format></format>
        </contentProperty>
        <contentProperty>
            <name>Doctor_Specialty</name>
            <expression>/dicom_image/doctor/specialties/specialty</expression>
            <type>STRING</type>
            <multivalued>true</multivalued>
            <format></format>
        </contentProperty>
        <contentProperty>
            <name>Followup_Needed</name>
            <expression>/dicom_image/followup_needed</expression>
            <type>BOOLEAN</type>
            <multivalued>false</multivalued>
            <format></format>
        </contentProperty>
        <contentProperty>
            <name>Image_Date</name>
            <expression>/dicom_image/image/date</expression>
            <type>DATE</type>
            <multivalued>false</multivalued>
            <format>MM/dd/yyyy</format>
        </contentProperty>
        <contentProperty>
            <name>Image_Type</name>
            <expression>/dicom_image/image/@type</expression>
            <type>STRING</type>
            <multivalued>false</multivalued>
            <format></format>
        </contentProperty>
        <contentProperty>
            <name>Patient_ID</name>
            <expression>/dicom_image/patient/id</expression>
            <type>INTEGER</type>
            <multivalued>false</multivalued>
            <format></format>
        </contentProperty>
        <contentProperty>
            <name>Patient_Name</name>
            <expression>/dicom_image/patient/name</expression>
            <type>STRING</type>
            <multivalued>false</multivalued>
            <format></format>
        </contentProperty>
    </contentProperties>
    <namespaces>
        <name>Medical-Records</name>
    </namespaces>
</contentClass>
```

## Request with cURL command line

```
curl -k -iT dicom.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://anytown-general-hospital.hcp.example.com:9090/mapi/tenants/
    anytown-general-hospital/contentClasses"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("dicom.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://anytown-general-hospital.hcp.example.com:9090/mapi/" +
  "tenants/anytown-general-hospital/contentClasses")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, os.path.getsize("dicom.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
PUT /mapi/tenants/anytown-general-hospital/contentClasses HTTP/1.1
Host: anytown-general-hospital.hcp.example.com:9090
Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6
Content-Type: application/xml
Content-Length: 2702
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Retrieving a list of content classes

Here’s a sample GET request that retrieves a list of the content classes defined for the Anytown-General-Hospital tenant. The request writes the list of content classes to a file named AGH-cc.xml. The request is made using a tenant-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -i -H "Accept: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://anytown-general-hospital.hcp.example.com:9090/mapi/tenants/
        anytown-general-hospital/contentClasses?prettyprint" > AGH-cc.xml
```

## Request in Python using PycURL

```
import pycurl
filehandle = open("AGH-cc.xml", 'wb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Accept: application/xml",\
  "Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://anytown-general-hospital.hcp.example.com:9090/mapi/" +
  "tenants/anytown-general-hospital/contentClasses")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.WRITEFUNCTION, filehandle.write)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
GET /mapi/tenants/anytown-general-hospital/contentClasses?prettyprint HTTP/1.1
Host: anytown-general-hospital.hcp.example.com:9090
Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6
Accept: application/xml
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 143
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<contentClasses>
    <name>Appointment</name>
    <name>DICOM</name>
</contentClasses>
```

### Erasure coding topology resources

Erasure coding topology resources let you create, specify tenants for, modify, retire, and delete erasure coding topologies. The table below provides information about these resources.

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| ecTopology | PUT | Create an erasure coding topology | System-level user account with the administrator role |  |
| List | GET | Retrieve a list of the existing erasure coding topologies | System-level user account with the monitor or administrator role | The listed erasure coding topologies are identified by erasure coding topology name.<br>In XML, the element that identifies each erasure coding topology is `name`. The root element for the list of erasure coding topologies is `ecTopologies`.<br>In JSON, the name in the name/value pair that lists the erasure coding topologies is `name`. |

Table. .../services/erasureCoding/ecTopologies

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| ecTopology | GET | Retrieve information about an erasure coding topology | System-level user account with the monitor or administrator role |  |
| N/A | HEAD | Check for the existence of an erasure coding topology | System-level user account with the monitor or administrator role |  |
| ecTopology | POST | Modify or retire an erasure coding topology | System-level user account with the administrator role |  |
| N/A | DELETE | Delete an erasure coding topology | System-level user account with the administrator role |  |

Table. .../services/erasureCoding/ecTopologies/ec-topology-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| tenant Candidates | GET | Retrieve a list of the tenants that are eligible to be added to an erasure coding topology | System-level user account with the monitor or administrator role |  |

Table. .../services/erasureCoding/ecTopologies/ec-topology-name/tenantCandidates

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| tenant Candidates | GET | Retrieve a list of tenants that are not eligible to be added to an erasure coding topology due to name or link conflicts | System-level user account with the monitor or administrator role |  |

Table. .../services/erasureCoding/ecTopologies/ec-topology-name/tenantConflictingCandidates

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the tenants that are included in a replication topology | System-level user account with the monitor or administrator role | In XML, the element that identifies each tenant is `name`. The root element for the list of tenants is `tenants`.<br>In JSON, the name in the name/value pair that lists the tenants is `name`. |

Table. .../services/erasureCoding/ecTopologies/ec-topology-name/tenants

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| String | PUT | Add a tenant to an erasure coding topology | System-level user account with the administrator role | With cURL, you need to use -X PUT, not -T, in a request to add a tenant to an erasure coding topology. |
| String | DELETE | Remove a tenant from an erasure coding topology | System-level user account with the administrator role |  |

Table. .../services/erasureCoding/ecTopologies/ec-topology-name/tenants/tenant-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| repliationLinks | GET | Retrieve a list of the replication links that may be eligible for use in an erasure coding topology that includes the local HCP system | System-level user account with the monitor or administrator role |  |

Table. .../services/erasureCoding/linkCandidates

#### Example: Retrieving a list of eligible replication links

Here's a sample GET request that retrieves a list of all the active/active replication links that directly connect the local HCP system to another HCP system or that are in a path of active/active links that indirectly connect the local system to another system. The request writes the list of links to a file named eligible-links.xml. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -H "Accept: application/xml"
    -H "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp-us.example.com:9090/mapi/services/erasureCoding/
        linkCandidates?verbose=true&prettyprint" > eligible-links.xml
```

## Request in Python using PycURL

```
import pycurl
filehandle = open("eligible-links.xml", 'wb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Accept: application/xml",\
  "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"])
curl.setopt(pycurl.URL,
  "https://admin.hcp-us.example.com:9090/mapi/services/" +
  "erasureCoding/linkCandidates?verbose=true&prettyprint")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.WRITEFUNCTION, filehandle.write)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
GET /mapi/services/erasureCoding/linkCandidates?verbose=true&prettyprint HTTP/1.1
Host: admin.hcp-us.example.com:9090
Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97
Accept: application/xml
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 2154
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<replicationLinks>
    <replicationLink>
        <hcpSystems>
            <name>hcp-ca.example.com</name>
            <name>hcp-eu.example.com</name>
        </hcpSystems>
        <name>eu-ca</name>
        <pausedTenantsCount>0</pausedTenantsCount>
        <state>HEALTHY</state>
        <uuid>7ae4101c-6e29-426e-ae71-9a7a529f019d</uuid>
    </replicationLink>
    <replicationLink>
        <hcpSystems>
            <name>hcp-ca.example.com</name>
            <name>hcp-us.example.com</name>
        </hcpSystems>
        <name>us-ca</name>
        <pausedTenantsCount>0</pausedTenantsCount>
        <state>HEALTHY</state>
        <uuid>cdb7edcd-feb6-4476-8d8d-bd053e3bc2ee</uuid>
    </replicationLink>
    <replicationLink>
        <hcpSystems>
            <name>hcp-an.example.com</name>
            <name>hcp-eu.example.com</name>
        </hcpSystems>
        <name>eu-an</name>
        <pausedTenantsCount>0</pausedTenantsCount>
        <state>HEALTHY</state>
        <uuid>77037ade-0115-4e30-a043-725f1bbcd87f</uuid>
    </replicationLink>
    <replicationLink>
        <hcpSystems>
            <name>hcp-eu.example.com</name>
            <name>hcp-us.example.com</name>
        </hcpSystems>
        <name>us-eu</name>
        <pausedTenantsCount>0</pausedTenantsCount>
        <state>HEALTHY</state>
        <uuid>32871da5-2355-458a-90f5-1717aa684d6f</uuid>
    </replicationLink>
    <replicationLink>
        <hcpSystems>
            <name>hcp-an.example.com</name>
            <name>hcp-us.example.com</name>
        </hcpSystems>
        <name>us-an</name>
        <pausedTenantsCount>0</pausedTenantsCount>
        <state>HEALTHY</state>
        <uuid>c8c875ad-dbfe-437d-abd3-862a6c719894</uuid>
    </replicationLink>
    <replicationLink>
        <hcpSystems>
            <name>hcp-an.example.com</name>
            <name>hcp-ca.example.com</name>
        </hcpSystems>
        <name>ca-an</name>
        <pausedTenantsCount>0</pausedTenantsCount>
        <state>HEALTHY</state>
        <uuid>a1f21e03-fb46-48cc-967e-b0cedf80bb20</uuid>
    </replicationLink>
</replicationLinks>
```

#### Example: Creating an erasure coding topology

Here's a sample PUT request that creates a four-system erasure coding topology named ex-corp-4. The topology definition is in an XML file named create-ect.xml. The request is made using a system-level user account that includes the administrator role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ecTopology>
    <name>ex-corp-4</name>
    <description>Erasure coding topology for the US, Europe, Canada, and Africa-North
        divisions.</description>
    <type>ring</type>
    <replicationLinks>
        <replicationLink>
            <name>us-eu</name>
        </replicationLink>
        <replicationLink>
            <name>eu-ca</name>
        </replicationLink>
        <replicationLink>
            <name>ca-an</name>
        </replicationLink>
        <replicationLink>
            <name>us-an</name>
        </replicationLink>
    </replicationLinks>
    <erasureCodingDelay>10</erasureCodingDelay>
    <fullCopy>false</fullCopy>
    <minimumObjectSize>4096</minimumObjectSize>
    <restorePeriod>5</restorePeriod>
</ecTopology>
```

## Request with cURL command line

```
curl -k -T create-ect.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp-us.example.com:9090/mapi/services/erasureCoding/
        ecTopologies"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("create-ect.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"])
curl.setopt(pycurl.URL,
  "https://admin.hcp-us.example.com:9090/mapi/services/" +
  "erasureCoding/ecTopologies")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, os.path.getsize("create-ect.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
PUT /mapi/services/erasureCoding/ecTopologies HTTP/1.1
Host: admin.hcp-us.example.com:9090
Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97
Content-Type: application/xml
Content-Length: 775
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Retrieving a list of eligible tenants

Here's a sample GET request that retrieves a list of all the local and remote tenants that are eligible to be included in the erasure coding topology named ex-corp-4. The request writes the list of tenants to a file named eligible-tenants.xml. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -H "Accept: application/xml"
    -H "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp-us.example.com:9090/mapi/services/erasureCoding/
        ecTopologies/ex-corp-4/tenantCandidates?verbose=true&prettyprint"
    > eligible-tenants.xml
```

## Request in Python using PycURL

```
import pycurl
filehandle = open("eligible-tenants.xml", 'wb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Accept: application/xml",\
  "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"])
curl.setopt(pycurl.URL,
  "https://admin.hcp-us.example.com:9090/mapi/services/" +
  "erasureCoding/ecTopologies/ex-corp-4/tenantCandidates" +
  "?verbose=true&prettyprint")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.WRITEFUNCTION, filehandle.write)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
GET /mapi/services/erasureCoding/ecTopologies/ex-corp-4/tenantCandidates
    ?verbose=true&prettyprint HTTP/1.1
Host: admin.hcp-us.example.com:9090
Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97
Accept: application/xml
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 976
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<tenantCandidates>
    <tenantCandidate>
        <hcpSystems>
            <name>hcp-us.example.com</name>
        </hcpSystems>
        <name>exec</name>
        <uuid>26112ed2-bb90-4e34-b450-4bf365a927d9</uuid>
    </tenantCandidate>
    <tenantCandidate>
        <hcpSystems>
            <name>hcp-us.example.com</name>
        </hcpSystems>
            <name>finance</name>
        <uuid>838cd575-0f94-489a-8f94-f36c1337c446</uuid>
    </tenantCandidate>
    <tenantCandidate>
        <hcpSystems>
            <name>hcp-eu.example.com</name>
        </hcpSystems>
        <name>research-dev</name>
        <uuid>38e7bc78-c700-4627-af63-a3c19eb77406</uuid>
    </tenantCandidate>
    <tenantCandidate>
        <hcpSystems>
            <name>hcp-us.example.com</name>
        </hcpSystems>
        <name>sales-mktg</name>
        <uuid>f349387f-18d0-49e2-9bcd-a8ac6f7952c5</uuid>
    </tenantCandidate>
</tenantCandidates>
```

#### Example: Adding a tenant to an erasure coding topology

Here's a sample GET request that adds a tenant named finance to an erasure coding topology named ex-corp-4. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -X PUT
    -H "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp-us.example.com:9090/mapi/services/erasureCoding/
        ecTopologies/ex-corp-4/tenants/finance"
```

## Request in Python using PycURL

```
import pycurl
import os
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Authorization: HCP \\
  bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"])
curl.setopt(pycurl.URL,
  "https://admin.hcp-us.example.com:9090/mapi/services/" +
  "erasureCoding/ecTopologies/ex-corp-4/tenants/finance")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
```

## Request headers

```
PUT /mapi/services/erasureCoding/ecTopologies/ex-corp-4/tenants/finance HTTP/1.1
Host: admin.hcp-us.example.com:9090
Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97
Accept:*/*
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Retrieving an erasure coding topology

Here's a sample GET request that retrieves an erasure coding topology named ex-corp-4. The request writes the output to a file named ex-corp-4.xml. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -H "Accept: application/xml"
    -H "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp-us.example.com:9090/mapi/services/erasureCoding/
        ecTopologies/ex-corp-4?verbose=true&prettyprint" > ex-corp-4.xml
```

## Request in Python using PycURL

```
import pycurl
filehandle = open("ex-corp-4.xml", 'wb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Accept: application/xml",\
  "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"])
curl.setopt(pycurl.URL,
  "https://admin.hcp-us.example.com:9090/mapi/services/" +
  "erasureCoding/ecTopologies/ex-corp-4?verbose=true&prettyprint")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.WRITEFUNCTION, filehandle.write)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
GET /mapi/services/erasureCoding/ecTopologies/ex-corp-4?verbose=true&prettyprint
    HTTP/1.1
Host: admin.hcp-us.example.com:9090
Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97
Accept: application/xml
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 2402
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ecTopology>
    <description>Erasure coding topology for the US, Europe, Canada, and
        Africa-North divisions.</description>
    <erasureCodedObjects>3289</erasureCodedObjects>
    <erasureCodingDelay>10</erasureCodingDelay>
    <fullCopy>false</fullCopy>
    <hcpSystems>
        <name>hcp-an.example.com</name>
        <name>hcp-ca.example.com</name>
        <name>hcp-eu.example.com</name>
        <name>hcp-us.example.com</name>
    </hcpSystems>
    <id>faa9b2e5-a8b0-4211-ac83-6a25dff50800</id>
    <minimumObjectSize>4096</minimumObjectSize>
    <name>ex-corp-4</name>
    <protectionStatus>HEALTHY</protectionStatus>
    <readStatus>HEALTHY</readStatus>
    <replicationLinks>
        <replicationLink>
            <hcpSystems>
                <name>hcp-ca.example.com</name>
                <name>hcp-eu.example.com</name>
            </hcpSystems>
            <name>eu-ca</name>
            <pausedTenantsCount>0</pausedTenantsCount>
            <state>HEALTHY</state>
            <uuid>7ae4101c-6e29-426e-ae71-9a7a529f019d</uuid>
        </replicationLink>
        <replicationLink>
            <hcpSystems>
                <name>hcp-eu.example.com</name>
                <name>hcp-us.example.com</name>
            </hcpSystems>
            <name>us-eu</name>
            <pausedTenantsCount>0</pausedTenantsCount>
            <state>HEALTHY</state>
            <uuid>32871da5-2355-458a-90f5-1717aa684d6f</uuid>
        </replicationLink>
        <replicationLink>
            <hcpSystems>
                <name>hcp-an.example.com</name>
                <name>hcp-us.example.com</name>
            </hcpSystems>
            <name>us-an</name>
            <pausedTenantsCount>0</pausedTenantsCount>
            <state>HEALTHY</state>
            <uuid>c8c875ad-dbfe-437d-abd3-862a6c719894</uuid>
        </replicationLink>
        <replicationLink>
            <hcpSystems>
                <name>hcp-an.example.com</name>
                <name>hcp-ca.example.com</name>
            </hcpSystems>
            <name>ca-an</name>
            <pausedTenantsCount>0</pausedTenantsCount>
            <state>HEALTHY</state>
            <uuid>a1f21e03-fb46-48cc-967e-b0cedf80bb20</uuid>
        </replicationLink>
    </replicationLinks>
    <restorePeriod>5</restorePeriod>
    <state>ACTIVE</state>
    <tenants>
        <name>research-dev</name>
        <name>sales-mktg</name>
        <name>exec</name>
        <name>finance</name>
    </tenants>
    <type>RING</type>
</ecTopology>
```

#### Example: Retiring an erasure coding topology

Here's a sample POST request that retires an erasure coding topology named ex-corp-4. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -d "<ecTopology/>"
    -H "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp-us.example.com:9090/mapi/services/erasureCoding/
        ecTopologies/ex-corp-4?retire"
```

## Request in Python using PycURL

```
import pycurl
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://admin.hcp-us.example.com:9090/mapi/services/" +
  "erasureCoding/ecTopologies/ex-corp-4?retire")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
```

## Request headers

```
POST /mapi/services/erasureCoding/ecTopologies/ex-corp-4?retire HTTP/1.1
Host: admin.hcp-us.example.com:9090
Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97
Accept: */*
Content-Length: 13
Content-Type: application/x-www-form-urlencoded
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

### Health check report resources

Health check report resources let you prepare, download, cancel, and retrieve the status of HCP health check reports.

## .../healthCheckReport

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| healthCheckDownloadStatus | GET | Retrieve the status of the health check reports download in progress. | System-level user with the administrator or service role |  |

## .../healthCheckReport/prepare

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| healthCheckPrepare | POST | Post a system request to prepare the health check reports. | System-level user with the administrator or service role | Health check reports can be prepared only on nodes that are running and available. |

## .../healthCheckReport/download

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| healthCheckDownload | POST | Post a system request to start the health check reports download. | System-level user with the administrator or service role | The health check reports archive can be downloaded at any time after the health check reports are successfully prepared. |

## .../healthCheckReport/cancel

This resource has no data types.

#### Example: Preparing health check reports for download

Here is a sample POST request that prepares health check reports for download. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -iX POST -d @HealthCheckPrepare.xml -k -H "Content-type: application/xml"
 -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
 "https://admin.hcp.example.com:9090/mapi/healthCheckReport/prepare"
```

## Request headers

```
POST /mapi/healthCheckReport/prepare HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP ZGVa3b9c17d52107f34fhdb83c7a5
Content-Type: application/xml
Content-Length: 37
```

## Example: Request body

```
<healthcheckPrepare>
     <startDate>09/19/2020</startDate>
     <endDate>09/21/2020</endDate>
     <exactTime>04:00</exactTime>
     <collectCurrent>false</collectCurrent>
</healthcheckPrepare>
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.2.27
Content-Length: 0
```

#### Example: Downloading the health check reports

Here is a sample POST request that downloads a health check reports archive. The request downloads the reports for HCP nodes 107 and 120, as specified in an XML file named HealthCheckDownload.xml. The request is made using a system-level user account that includes the administrator role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<healthCheckDownload>
     <nodes>107,120</nodes>
     <content>HCR</content>
</healthCheckDownload>
```

## Request with cURL command line

```
curl -X POST -T HealthCheckDownload.xml -H
"Content-Type: application/xml" -k -b hcp-api-auth="FL3Z:a3b9c17d52107f34fhdb83c7a5"
https://admin.hcp.example.com:9090/mapi/healthCheckReport/download --output HCR_LOGS.zip
```

## Request headers

```
POST /mapi/healthCheckReport/download HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP ZGVa3b9c17d52107f34fhdb83c7a5
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.1.0
Content-Length: 575
```

#### Example: Retrieving the health check reports download status

Here is a sample GET request that retrieves the health check reports download status. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -b hcp-api-auth="HCP ZGVa3b9c17d52107f34fhdb83c7a5"
 https://admin.hcp.example.com:9090/mapi/healthCheckReport?prettyprint
```

## Request headers

```
GET /mapi/healthCheckReport HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP ZGVa3b9c17d52107f34fhdb83c7a5
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.2.27
Content-Length: 383
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<healthCheckDownloadStatus>
     <readyForStreaming>true</readyForStreaming>
     <streamingInProgress>false</streamingInProgress>
     <error>false</error>
     <started>true</started>
     <content>HCR</content>
</healthCheckDownloadStatus>
```

#### Example: Canceling the health check reports

Here is a sample POST request that cancels the health check reports download. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -X POST -k -b hcp-api-auth="HCP ZGVa3b9c17d52107f34fhdb83c7a5"
 -H "Accept: application/xml" https://admin.hcp.example.com:9090/mapi/healthCheckReport?cancel
```

## Request headers

```
POST /mapi/healthCheckReport?cancel HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP ZGVa3b9c17d52107f34fhdb83c7a5
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.1.0
Content-Length: 0
```

### License resources

License resources let you upload a license key and retrieve the current license or a list of the current and past licenses.

## .../storage/licenses

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| Licenses | GET | Retrieve either the current storage license or a list of the current and past storage licenses | System-level user account with the monitor or administrator role | HCP does not return information about licensing of S Series storage.<br>To display the list of current and past storage licenses, use the verbose query parameter. |
| N/A | PUT | Upload a new storage license | System-level user account with the administrator role | With the PUT request, you need to supply a the license text key string. |

#### Example: Retrieving a premium storage license list

Here is a sample GET request that retrieves the current premium storage license information. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -i -H "Accept: application/xml"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/storage/licenses?prettyprint"
```

## Request in Python using PycURL

```
import pycurl
filehandle = open("HCPLic_SN12345_Q0123456789_A10TB_01-01-2021.plk",
  'wb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Accept: application/xml",\
  "Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
  "https://admin.hcp.example.com:9090/mapi/storage/licenses?" +
  "prettyprint")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.WRITEFUNCTION, filehandle.write)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
GET /mapi/storage/licenses?prettyprint HTTP/1.1
Host: admin.hcp.example.com:9090
Accept: application/xml
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 356
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<licenses>
     <license>
         <activeCapacity>10000000000000</activeCapacity>
         <expirationDate>Jan 1, 2025</expirationDate>
         <extendedCapacity>0</extendedCapacity>
         <licenseType>Premium</licenseType>
         <serialNumber>12345</serialNumber>
         <uploadDate>Aug 14, 2020</uploadDate>
     </license>
</licenses>
```

#### Example: Uploading a new license

Here’s a sample PUT request that uploads a storage license. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -i -k -H "Content-Type: text/html"
    -H "Authorization=HCP YWxscm9sZXM=:04ec9f614d89ff5c7126d32acb448382"
    -X PUT -d
    "450a4346cac1d4d49c719312caed27f4f582ba1024b342a0f10be7a7283e6f8acba69e5c"
    "https://admin.hcp.example.com:9090/mapi/storage/licenses"
```

## Request in Python using PycURL

```
import pycurl
import os
readString = "450a4346cac1d4d49c719312caed27f4f582ba1024b342a0f10be7a7283e6f8acba69e5c",
curl = pycurl.Curl()
curl.setopt(curl.HTTPHEADER, ["Content-Type: text/html", "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
  "https://admin.hcp.example.com:9090/mapi/storage/licenses”)
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.CUSTOMREQUEST, "PUT")
curl.setopt(pycurl.POSTFIELDS, readString)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
PUT /mapi/services/replication/links HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Content-Type: text/html
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

### Log resources

Log resources let you prepare, download, and monitor the download of the HCP internal logs.

## .../logs

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| LogDownloadStatus | GET | Retrieve the status of the log download in progress | System-level user with the administrator, service, or monitor role. |  |
| N/A | POST | Marks the logs with a supplied message | System-level user with the administrator or service role. | You supply the message at the end of the command. Use the plus (+) sign or %20 to make spaces between words. |
| N/A | POST | Clears the log download so that it can be reinitiated. | System-level user with the administrator or service role. |  |

## .../logs/prepare

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| LogPrepare | POST | Post a system request to package logs for download | System-level user with the administrator or service role. | Packages all logs, regardless of log type. If prepared logs are not downloaded withing twenty four hours, the package is deleted. |

## .../logs/download

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| LogDownload | POST | Starts the log download | System-level user with the administrator or service role. | Downloaded logs come as .zip files. For HCP nodes, you have control over which log types are downloaded. |

#### Example: Start log packaging

Here’s a sample POST request that packages system logs for download. The command requests to package logs between the dates of 8/19/2017 and 8/21/2017. The dates are specified in an XML file named logPrepare.xml. The request is made using a system-level user account that includes the administrator role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<logPrepare>
    <startDate>08/19/2017</startDate>
    <endDate>08/21/2017</endDate>
</logPrepare>
```

## Request with cURL command line

```
curl -iX POST -d @logPrepare.xml -k -i -H "Content-type: application/xml"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/logs/prepare"
```

## Request in Python using PycURL

```
import pycurl
import os
filename = "logPrepare.xml"
filehandle = open(filename, "rb")
filesize = os.path.getsize(filename)
curl = pycurl.Curl()
curl.setopt(pycurl.VERBOSE, True)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml", \\
"Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL, "https://admin.hcp.example.com:9090/mapi/logs/prepare")
curl.setopt(pycurl.SSL_VERIFYPEER, False)
curl.setopt(pycurl.SSL_VERIFYHOST, False)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, filesize)
curl.perform()
filehandle.close()
curl.close()
```

## Request headers

```
POST /mapi/logs/prepare HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Content-Type: application/xml
Content-Length: 56
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Download the logs

Here’s a sample POST request that downloads the system logs prepared in the previous example to your current directory in a zip file. The request downloads the Service logs for General Node 17. The log type and selected General Node are specified in an XML file named logDownload.xml. The request is made using a system-level user account that includes the administrator role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<logDownload>
    <nodes>17</nodes>
    <content>SERVICE</content>
</logDownload>
```

## Request with cURL command line

```
curl -X POST -T logDownload.xml -k -H "Content-type: application/xml"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/logs/download"
    -o logDownload.zip
```

## Request in Python using PycURL

```
import pycurl
import os
filename = "logDownload.xml"
filehandle = open(filename, "rb")
filesize = os.path.getsize(filename)
output = open("downloadedLogs.zip", "wb")
curl = pycurl.Curl()
curl.setopt(pycurl.VERBOSE, True)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml", \\
"Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL, "https://admin.hcp.example.com:9090/mapi/logs/download")
curl.setopt(pycurl.SSL_VERIFYPEER, False)
curl.setopt(pycurl.SSL_VERIFYHOST, False)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, filesize)
curl.setopt(pycurl.WRITEFUNCTION, output.write)
curl.perform()
print(curl.getinfo(pycurl.RESPONSE_CODE))
filehandle.close()
curl.close()
```

## Request headers

```
POST /mapi/logs/download HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/zip
Content-Disposition: attachment; filename=HCPLogs-admin.hcp.example.com-n17-sp20170321-1225.zip
Accept-Ranges: none
Transfer-Encoding: chunked
```

#### Example: Retrieving the log download status

Here’s a sample GET request that retrieves the log download status. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -i
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/logs"
```

## Request in Python using PycURL

```
import pycurl
curl = pycurl.Curl()
curl.setopt(pycurl.VERBOSE, True)
curl.setopt(pycurl.CUSTOMREQUEST, "GET")
curl.setopt(pycurl.HTTPHEADER, ["Accept: application/xml", \\
"Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL, "https://admin.hcp.example.com:9090/mapi/logs?prettyprint")
curl.setopt(pycurl.SSL_VERIFYPEER, False)
curl.setopt(pycurl.SSL_VERIFYHOST, False)
curl.perform()
curl.close()
```

## Request headers

```
GET /mapi/logs HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 282
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<logDownloadStatus>
    <readyForStreaming>true</readyForStreaming>
    <streamingInProgress>false</streamingInProgress>
    <started>true</started>
    <error>false</error>
    <content>SERVICE</content>
</logDownloadStatus>
```

#### Example: Canceling a log download

Here’s a sample POST request that clears the log download status. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -X POST -k -i
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/logs?cancel"
```

## Request in Python using PycURL

```
import pycurl
curl = pycurl.Curl()
curl.setopt(pycurl.VERBOSE, True)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.setopt(pycurl.HTTPHEADER, ["Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL, "https://admin.hcp.example.com:9090/mapi/logs?cancel")
curl.setopt(pycurl.SSL_VERIFYPEER, False)
curl.setopt(pycurl.SSL_VERIFYHOST, False)
curl.perform()
curl.close()
```

## Request headers

```
POST /mapi/logs?cancel HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Marking the download log

Here’s a sample POST request that marks the system logs with the message `Mark the log.` The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -X POST -k -i
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/logs?mark=Mark+the+log"
```

## Request in Python using PycURL

```
import pycurl
markMessage = "Mark+the+log"
curl = pycurl.Curl()
curl.setopt(pycurl.VERBOSE, True)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.setopt(pycurl.HTTPHEADER, ["Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL, ("https://admin.hcp.example.com:9090/mapi/logs/?" +
  "mark="Mark+the+log"))
curl.setopt(pycurl.SSL_VERIFYPEER, False)
curl.setopt(pycurl.SSL_VERIFYHOST, False)
curl.perform()
curl.close()
```

## Request headers

```
POST /mapi/logs?mark=Mark+the+log HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

### Namespace resources

Namespace resources let you create, retrieve information about, modify, and delete namespaces. The table below provides information about these resources.

## .../tenants/tenant-name/namespaces

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| namespace | PUT | Create an HCP namespace | Tenant-level user account with the administrator role or allow namespace management property | Not valid for the default namespace. |
| List | GET | Retrieve a list of the namespaces owned by a tenant | - For an HCP tenant, tenant-level user account with the monitor, administrator, or compliance role or allow namespace management property<br>- For the default tenant, system-level user account with the monitor or administrator role | The listed namespaces are identified by namespace name.<br>In XML, the element that identifies each namespace is `name`. The root element for the list of namespaces is `namespaces`.<br>In JSON, the name in the name/value pair that lists the namespaces is `name`.<br>For a user with the allow namespace management property and no roles, the retrieved list includes only the namespaces the user owns. |

## .../tenants/tenant-name/namespaces/cors

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| cors | PUT | Set the CORS rules configuration for a namespace | - System-level user account with the administrator role<br>- Tenant-level user account with the administrator role | A CORS configuration set on a namespace overrides the default tenant-level CORS configuration. |
| GET | Retrieve the CORS configuration for a namespace | - System-level user account with the monitor or administrator role<br>- Tenant-level user account with the monitor or administrator role |  |
| DELETE | Delete the CORS configuration for a namespace | - System-level user account with the administrator role<br>- Tenant-level user account with the administrator role | If a CORS configuration is not set on the namespace, the HTTP status code `404 Not Found` is returned. |

## .../tenants/tenant-name/namespaces/namespace-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| namespace | GET | Retrieve information about a namespace | - For an HCP namespace, tenant-level user account with the monitor or administrator role or allow namespace management property<br>- For the default namespace, system-level user account with the monitor or administrator role | A user with the allow namespace management property and no roles can retrieve information only about the namespaces the user owns. In this case, the retrieved information includes only the namespace name and owner. |
| N/A | HEAD | Check for the existence of a namespace | - For an HCP namespace, tenant-level user account with the monitor, administrator, or compliance role or allow namespace management property<br>- For the default namespace, system-level user account with the monitor or administrator role |  |
| namespace | POST | Modify a namespace | - For an HCP namespace, tenant-level user account with the administrator role<br>- For the default namespace, system-level user account with the administrator role |  |
| N/A | DELETE | Delete an HCP namespace | Tenant-level user account with the administrator role or allow namespace management property | A user with the allow namespace management property and no roles can delete only the namespaces the user owns.<br>The namespace cannot contain any objects.<br>Not valid for the default namespace. |

## .../tenants/tenant-name/namespaces/namespace-name/chargebackReport

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| chargeback<br>Report | GET | Generate a chargeback report for a namespace | Tenant-level user account with the monitor or administrator role | Not valid for the default namespace.<br>Supported output formats are XML, JSON, and CSV. |

## .../tenants/tenant-name/namespaces/namespace-name/complianceSettings

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| compliance Settings | GET | Retrieve the default retention, shred, custom metadata handling, and disposition settings for a namespace | - For an HCP namespace, tenant-level user account with the monitor, administrator, or compliance role<br>- For the default namespace, system-level user account with the monitor, administrator, or compliance role | Default retention and shred settings do not apply to the default namespace. |
| POST | Modify the default retention, shred, custom metadata handling, or disposition setting for a namespace | - For an HCP namespace, tenant-level user account with the compliance role<br>- For the default namespace, system-level user account with the compliance role |

## ../tenants/tenant-name/namespaces/namespace-name/customMetadataIndexingSettings

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| custom Metadata Indexing Settings | GET | Retrieve settings specific to metadata query engine indexing of custom metadata for a search-enabled namespace | - For an HCP namespace, tenant-level user account with the monitor or administrator role<br>- For the default namespace, system-level user account with the monitor or administrator role | Not valid for namespaces that do not have search enabled. |
| POST | Modify settings specific to metadata query engine indexing of custom metadata for a search-enabled namespace | - For an HCP namespace, tenant-level user account with the administrator role<br>- For the default namespace, system-level user account with the administrator role |

## .../tenants/tenant-name/namespaces/namespace-name/permissions

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve the list of permissions in the data access permission mask for a namespace | - For an HCP namespace, tenant-level user account with the monitor or administrator role<br>- For the default namespace, system-level user account with the monitor or administrator role |  |
| List | POST | Modify the list of permissions in the data access permission mask for a namespace | - For an HCP namespace, tenant-level user account with the administrator role<br>- For the default namespace, system-level user account with the administrator role | Valid values for permissions are:<br>- DELETE<br>- PRIVILEGED<br>- PURGE<br>- READ<br>- SEARCH<br>- WRITE<br>These values are case sensitive.<br>The set of permissions specified in the request body replaces the set of permissions currently included in the data access permission mask for the namespace. To remove all permissions, specify an empty list.<br>If the set of permissions includes `PURGE`, delete permission is enabled automatically. If the set of permissions includes `SEARCH`, read permission is enabled automatically.<br>By default, when you create a namespace, its data access permission mask includes all permissions. |

## .../tenants/tenant-name/namespaces/namespace-name/protocols

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| protocols | GET | Retrieve a subset of the HTTP namespace access protocol settings for the default namespace | System-level user account with the monitor or administrator role | Not valid for HCP namespaces. For HCP namespaces, this resource has been superseded by the .../protocols/http resource. |
| POST | Modify a subset of the HTTP namespace access protocol settings for the default namespace | System-level user account with the administrator role |

## .../tenants/tenant-name/namespaces/namespace-name/protocols/protocol-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| Determined by protocol-name. Possible data types are:<br> <br>- cifsProtocol<br>- httpProtocol<br>- nfsProtocol<br>- smtpProtocol | GET | Retrieve the applicable namespace access protocol settings for a namespace | Tenant-level user account with the monitor or administrator role | Not valid for the default namespace.<br>Valid values for protocol-name are:<br>- cifs<br>- http<br>- nfs<br>- smtp<br>These values are case sensitive.<br>The httpProtocol data type includes properties for both the HTTP and WebDAV protocols. |
| POST | Modify the applicable namespace access protocol settings for a namespace | Tenant-level user account with the administrator role |

## .../tenants/tenant-name/namespaces/namespace-name/replicationCollisionSettings

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| replication Collision Settings | GET | Retrieve the replication collision handling settings for a namespace | - For an HCP namespace, tenant-level user account with the monitor or administrator role<br>- For the default namespace, system-level user account with the monitor or administrator role |  |
| POST | Modify the replication collision handling settings for a namespace | - For an HCP namespace, tenant-level user account with the administrator role<br>- For the default namespace, system-level user account with the administrator role |  |

## .../tenants/tenant-name/namespaces/namespace-name/statistics

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| statistics | GET | Retrieve information about the content of a namespace | - For an HCP namespace, tenant-level user account with the monitor or administrator role<br>- For the default namespace, system-level user account with the monitor or administrator role |  |

## .../tenants/tenant-name/namespaces/namespace-name/versioningSettings

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| versioning<br>Settings | GET | Retrieve the versioning settings for a namespace | Tenant-level user account with the monitor or administrator role or the allow namespace management property | A user with the allow namespace management property and no roles can retrieve and modify only the versioning enabled property and that property only for the namespaces the user owns.<br>Not valid for the default namespace. |
| POST | Modify the versioning settings for a namespace | Tenant-level user account with the administrator role or the allow namespace management property |
| DELETE | Delete the versioning settings for a namespace | Tenant-level user account with the monitor or administrator role or the allow namespace management property |

#### Example: Creating an HCP namespace

Here’s a sample PUT request that creates an HCP namespace named Accounts-Receivable for the Finance tenant. The namespace definition is in an XML file named AccountsRecNS.xml. The request is made using a tenant-level user account that includes the administrator role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<namespace>
    <name>Accounts-Receivable</name>
    <description>Created for the Finance department at Example Company by Lee
        Green on 2/9/2017.</description>
    <hashScheme>SHA-256</hashScheme>
    <enterpriseMode>true</enterpriseMode>
    <hardQuota>50 GB</hardQuota>
    <softQuota>75</softQuota>
    <servicePlan>Short-Term-Activity</servicePlan>
    <optimizedFor>CLOUD</optimizedFor>
    <versioningSettings>
        <enabled>true</enabled>
        <prune>true</prune>
        <pruneDays>10</pruneDays>
        <useDeleteMarkers>true</useDeleteMarkers>
    </versioningSettings>
    <multipartUploadAutoAbortDays>10</multipartUploadAutoAbortDays>
    <searchEnabled>true</searchEnabled>
    <indexingEnabled>true</indexingEnabled>
    <customMetadataIndexingEnabled>true</customMetadataIndexingEnabled>
    <customMetadataValidationEnabled>true</customMetadataValidationEnabled>
    <replicationEnabled>true</replicationEnabled>
    <allowErasureCoding>true</allowErasureCoding>
    <readFromReplica>true</readFromReplica>
    <serviceRemoteSystemRequests>true</serviceRemoteSystemRequests>
    <tags>
        <tag>Billing</tag>
        <tag>lgreen</tag>
    </tags>
</namespace>
```

## Request with cURL command line

```
curl -k -iT AccountsRecNS.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("AccountsRecNS.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
"namespaces")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, os.path.getsize("AccountsRecNS.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
PUT /mapi/tenants/finance/namespaces HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6
Content-Type: application/xml
Content-Length: 1197
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example Changing the compliance settings for an HCP namespace

Here’s a sample POST request that changes the compliance settings for the Accounts-Receivable namespace. The new settings are in an XML file named AR-compliance.xml. The request is made using a tenant-level user account that includes the compliance role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<complianceSettings>
    <retentionDefault>A+7y</retentionDefault>
    <minimumRetentionAfterInitialUnspecified>19y+0M+7d</minimumRetentionAfterInitialUnspecified>
    <shreddingDefault>false</shreddingDefault>
    <customMetadataChanges>all</customMetadataChanges>
    <dispositionEnabled>true</dispositionEnabled>
</complianceSettings>
```

## Request with cURL command line

```
curl -k -i -d @AR-compliance.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP bXdoaXRl:ad49ce36d0cec9634ef63b24151be0fb"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces/
         accounts-receivable/complianceSettings"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("AR-compliance.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  bXdoaXRl:ad49ce36d0cec9634ef63b24151be0fb"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
 "namespaces/accounts-receivable/complianceSettings")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.setopt(pycurl.INFILESIZE,
  os.path.getsize("AR-compliance.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
POST /mapi/tenants/finance/namespaces/accounts-receivable/complianceSettings
  HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bXdoaXRl:ad49ce36d0cec9634ef63b24151be0fb
Content-Type: application/xml
Content-Length: 285
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example Configuring the REST API for an HCP namespace

Here’s a sample POST request that configures the REST API for the Accounts-Receivable namespace. The new settings are in an XML file named AR-http.xml. The request is made using a tenant-level user account that includes the administrator role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<httpProtocol>
    <httpsEnabled>true</httpsEnabled>
    <httpEnabled>false</httpEnabled>
    <restEnabled>true</restEnabled>
    <restRequiresAuthentication>true</restRequiresAuthentication>
    <httpActiveDirectorySSOEnabled>true</httpActiveDirectorySSOEnabled>
    <ipSettings>
         <allowAddresses>
             <ipAddress>192.168.140.10</ipAddress>
             <ipAddress>192.168.140.14</ipAddress>
             <ipAddress>192.168.140.15</ipAddress>
             <ipAddress>192.168.149.0/24</ipAddress>
         </allowAddresses>
         <allowIfInBothLists>false</allowIfInBothLists>
         <denyAddresses>
             <ipAddress>192.168.149.5</ipAddress>
         </denyAddresses>
    </ipSettings>
</httpProtocol>
```

## Request with cURL command line

```
curl -k -i -d @AR-http.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces/
         accounts-receivable/protocols/http"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("AR-http.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
  "namespaces/accounts-receivable/protocols/http")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.setopt(pycurl.INFILESIZE,
   os.path.getsize("AR-http.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
POST /mapi/tenants/finance/namespaces/accounts-receivable/protocols/http
    HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6
Content-Type: application/xml
Content-Length: 285
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

### Network resources

Network resources let you activate advanced DNS configuration mode and view the current DNS configuration setting. The table below provides information about these resources.

## .../network

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| Network settings | GET | Retrieves the current downstream DNS mode. | System-level user account with the monitor or administrator role |  |
| POST | Modifies the current downstream DNS mode setting for the given network. | System-level user account with the administrator role | Valid values for this setting are:<br>- ADVANCED<br>- BASIC |

#### Example: Enabling advanced downstream DNS configuration

Here’s a sample POST request that sets the downstream DNS mode to advanced. The DNS mode is specified in an XML file named network.xml. The request is made using a system-level user account that has the administrator role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<networkSettings>
    <downstreamDNSMode>ADVANCED</downstreamDNSMode>
</networkSettings>
```

## Request with cURL command line

```
curl -k -i -d @network.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP YWRtaW4=:e00cf25ad42683b3df678c61f42c6bda"
    "https://admin.hcp.example.com:9090/mapi/network"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("network.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml", / "Authorization: HCP YWRtaW4=:e00cf25ad42683b3df678c61f42c6bda"])
curl.setopt(pycurl.URL,
  "https://admin.hcp.example.com:9090/mapi/network"
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, os.path.getsize("network.xml")) curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
POST /mapi/network?downstreamDNSMode=ADVANCED HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWRtaW4=:e00cf25ad42683b3df678c61f42c6bda
Content-Type: application/xml
Content-Length: 141
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Checking the advanced DNS configuration

Here’s a sample GET request that lists whether the advanced DNS configuration mode is enabled or disabled. The request is made using a system-level user account that has the administrator role.

## Request with cURL command line

```
curl -k -H "Content-Type: application/xml"
    -H "Authorization: HCP YWRtaW4=:e00cf25ad42683b3df678c61f42c6bda"
    "https://admin.hcp.example.com:9090/mapi/network”
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("network.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml", / "Authorization: HCP YWRtaW4=:e00cf25ad42683b3df678c61f42c6bda"])
curl.setopt(pycurl.URL,
  "https://admin.hcp.example.com:9090/mapi/network"
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.CUSTOMREQUEST, "GET")
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, os.path.getsize("network.xml")) curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
GET /mapi/network HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWRtaW4=:e00cf25ad42683b3df678c61f42c6bda
Content-Type: application/xml
Content-Length: 152
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<networkSettings>
    <downstreamDNSMode>BASIC</downstreamDNSMode>
</networkSettings>
```

### Node statistics resource

The node statistics resource lets you retrieve the statistical information of nodes in your HCP system.

Note: A best practice is to limit your HTTP GET requests for retrieving statistics and metrics to once per hour. Polling the system more frequently can lead to system instability.


## .../nodes/statistics

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| nodeStatistics | GET | Retrieve the statistics of nodes in the HCP system | System-level user with the administrator or monitor role. |  |

#### Example: Retrieving node statistics

Here is a sample request that retrieves node statistics for nodes in the system. In this example, statistics are collected on two nodes and two volumes on each node. Also, the management port network has been configured on each node.

## Request with cURL command line

```
curl -ik -H "Authorization: HCP ZGVa3b9c17d52107f34fhdb83c7a5" -H "Accept: application/xml" "https://admin.hcp.example.com:9090/mapi/nodes/statistics?prettyprint"
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<nodeStatistics>
     <requestTime>1528292517330</requestTime>
     <nodes>
          <node>
               <nodeNumber>17</nodeNumber>
               <frontendIpAddresses>
                    <ipAddress>172.20.35.17</ipAddress>
                    <ipAddress>2001:623:0:0:222:11ff:fec7:6584</ipAddress>
               </frontendIpAddresses>
               <backendIpAddress>172.35.14.17</backendIpAddress>
               <managementIpAddresses>
                    <ipAddress>172.20.45.17</ipAddress>
                    <ipAddress>2001:623:0:0:222:11ff:fec7:6585</ipAddress>
               </managementIpAddresses>
               <openHttpConnections>0</openHttpConnections>
               <openHttpsConnections>0</openHttpsConnections>
               <maxHttpConnections>255</maxHttpConnections>
               <maxHttpsConnections>254</maxHttpsConnections>
               <cpuUser>0.16</cpuUser>
               <cpuSystem>0.08</cpuSystem>
               <cpuMax>24</cpuMax>
               <ioWait>0.02</ioWait>
               <swapOut>0.0</swapOut>
               <maxFrontEndBandwidth>1024000</maxFrontEndBandwidth>
               <frontEndBytesRead>0.3</frontEndBytesRead>
               <frontEndBytesWritten>0.2</frontEndBytesWritten>
               <maxBackEndBandwidth>1024000</maxBackEndBandwidth>
               <backEndBytesRead>7.22</backEndBytesRead>
               <backEndBytesWritten>3.87</backEndBytesWritten>
               <maxManagementPortBandwidth>1024000</maxManagementPortBandwidth>
               <managementBytesRead>.75</managementBytesRead>
               <managementBytesWritten>.7</managementBytesWritten>
               <collectionTimestamp>1528292472000</collectionTimestamp>
               <volumes>
                    <volume>
                         <id>example090</id>
                         <blocksRead>0.0</blocksRead>
                         <blocksWritten>24.8</blocksWritten>
                         <diskUtilization>0.0</diskUtilization>
                         <transferSpeed>0.57</transferSpeed>
                         <totalBytes>10217599</totalBytes>
                         <freeBytes>37887380</freeBytes>
                         <totalInodes>10443212</totalInodes>
                         <freeInodes>40083820</freeInodes>
                    </volume>
                    <volume>
                         <id>example091</id>
                         <blocksRead>138.93</blocksRead>
                         <blocksWritten>34.0</blocksWritten>
                         <diskUtilization>0.48</diskUtilization>
                         <transferSpeed>7.82</transferSpeed>
                         <totalBytes>10223583</totalBytes>
                         <freeBytes>37940648</freeBytes>
                         <totalInodes>10223616</totalInodes>
                         <freeInodes>38073632</freeInodes>
                    </volume>
               </volumes>
          </node>
          <node>
               <nodeNumber>173</nodeNumber>
               <frontendIpAddresses>
                    <ipAddress>172.20.35.16</ipAddress>
                    <ipAddress>2001:623:0:0:222:11ff:fec7:6574</ipAddress>
               </frontendIpAddresses>
               <backendIpAddress>172.35.14.16</backendIpAddress>
               <managementIpAddresses>
                    <ipAddress>172.20.45.16</ipAddress>
                    <ipAddress>2001:623:0:0:222:11ff:fec7:6575</ipAddress>
               </managementIpAddresses>
               <openHttpConnections>0</openHttpConnections>
               <openHttpsConnections>0</openHttpsConnections>
               <maxHttpConnections>255</maxHttpConnections>
               <maxHttpsConnections>254</maxHttpsConnections>
               <cpuUser>0.06</cpuUser>
               <cpuSystem>0.06</cpuSystem>
               <cpuMax>24</cpuMax>
               <ioWait>0.0</ioWait>
               <swapOut>0.0</swapOut>
               <maxFrontEndBandwidth>1024000</maxFrontEndBandwidth>
               <frontEndBytesRead>0.17</frontEndBytesRead>
               <frontEndBytesWritten>0.1</frontEndBytesWritten>
               <maxBackEndBandwidth>1024000</maxBackEndBandwidth>
               <backEndBytesRead>5.2</backEndBytesRead>
               <backEndBytesWritten>2.7</backEndBytesWritten>
               <maxManagementPortBandwidth>1024000</maxManagementPortBandwidth>
               <managementBytesRead>.32</managementBytesRead>
               <managementBytesWritten>.27</managementBytesWritten>
               <collectionTimestamp>1528292486000</collectionTimestamp>
               <volumes>
                    <volume>
                         <id>example092</id>
                         <blocksRead>0.0</blocksRead>
                         <blocksWritten>6.8</blocksWritten>
                         <diskUtilization>0.0</diskUtilization>
                         <transferSpeed>0.27</transferSpeed>
                         <totalBytes>10473796</totalBytes>
                         <freeBytes>38456396</freeBytes>
                         <totalInodes>10458232</totalInodes>
                         <freeInodes>36383276</freeInodes>
                    </volume>
                    <volume>
                         <id>example093</id>
                         <blocksRead>13.93</blocksRead>
                         <blocksWritten>28.0</blocksWritten>
                         <diskUtilization>0.08</diskUtilization>
                         <transferSpeed>1.82</transferSpeed>
                         <totalBytes>10223583</totalBytes>
                         <freeBytes>37940648</freeBytes>
                         <totalInodes>10498423</totalInodes>
                         <freeInodes>38073632</freeInodes>
                    </volume>
               </volumes>
          </node>
     </nodes>
</nodeStatistics>
```

### Paging, sorting, and filtering

By default, in response to a GET request for:

- The namespaces resource, HCP returns a list of all the namespaces owned by the applicable tenant
- The userAccounts resource, HCP returns a list of all the user accounts defined for the applicable tenant
- The dataAccessPermissions resource, HCP returns a list of all the namespaces for which the applicable user account or group account has any permissions, along with the permissions granted for each of those namespaces

With very large numbers of these items, such requests can overload or reduce the efficiency of the client. Additionally, if you’re interested in only a small number of the listed items, finding the information you want can be difficult.

To manage the results of GET requests, you can use query parameters to page through, sort, and filter these resource lists.

#### Paging through resource lists

You can limit the number of items HCP returns in response to an individual GET request by specifying an offset into the applicable list and a count of the items to return. By issuing multiple such requests, you can retrieve the entire list, one manageable number of items at a time. This is called paging.

To specify the offset and count, you use these query parameters in the GET request:

offset=offsetspecifies the offset of the first item to include in the returned tenant or namespace list.count=countspecifies the number of items to return.

These considerations apply to paging through resource lists:

- The offset and count parameters are valid only with the namespaces, userAccounts, and dataAccessPermissions resources.
- The first item in the complete list is at offset zero. So, for example, to retrieve the first five items, you would specify `offset=0` and `count=5`. To retrieve the sixth through tenth items, you would specify `offset=5` and `count=5`.
- If you omit the offset parameter, the returned list starts with the item at offset zero.
- If you specify an offset that is greater than or equal to the number of items in the complete list, no items are returned.
- If you omit the count parameter, the returned list includes all items starting from the offset and continuing through the end of the complete list.
- If you specify a count that is greater than the number of items remaining in the complete list after the offset, the returned list includes all items starting from the offset and continuing through the end of the complete list.
- If an item is deleted while you are paging through the applicable list, the full list of items returned may be incomplete. For example, suppose a tenant has six namespaces (A, B, C, D, E, and F):
1. You issue a GET request for the namespaces resource with these query parameters: `offset=0&count=3`
     HCP returns A, B, and C.

2. Namespace B is deleted without your knowledge.

     The remaining namespaces are A, C, D, E, and F.

3. You issue a second GET request for the namespaces resource with these query parameters: `offset=3&count=3`
     HCP returns E and F. It does not return D because D is now at offset two.
- You can page, sort, and filter resource lists in the same request.

Tip: By default, resource lists are returned in an arbitrary order, and the order may not be the same if the request is issued more than once. To ensure that you get all the expected items when paging, specify a sort order in each request.


#### Sorting resource lists

You can retrieve a sorted list of items of a particular resource type by specifying the property you want to sort by and the sort order in the GET request. You can sort namespaces by name or by hard quota. You can sort user accounts by username.

To specify a sort property and sort order, you use these query parameters in the GET request:

sortType=property-name
specifies the property you want to sort by. Valid values are:


- For the namespaces and dataAccessPermissions resources, `name` and `hardQuota`
- For the userAccounts resource, `username`

This parameter is optional. For the namespaces and dataAccessPermissions resources, the default is `name`.
sortOrder=(ascending\|descending)
specifies the order in which to sort the listed items.
This parameter is optional. The default is `ascending`.


For example, this GET request sorts the list of in descending order by hard quota:

```
curl -k -i -H "Accept: application/xml"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"    "https://admin.hcp.example.com:9090/mapi/tenants/finance/namespaces?
         sortType=hardQuota&sortOrder=descending&prettyprint"
```

These considerations apply to sorting resource lists:

- The sortType and sortOrder parameters are valid only with the namespaces, userAccounts, and dataAccessPermissions resources.
- You can page, sort, and filter resource lists in the same request.

#### Filtering resource lists

You can retrieve a subset of items of a particular resource type by specifying a filter in the GET request. To apply a filter, you specify the property you want to filter by and the text string to use as the filter. The filtered list includes only those items for which the value of the specified property begins with or is the same as the specified text string.

You can filter namespaces by name or by tag. You can filter user accounts by username.

To specify a filter, you use these query parameters in the GET request:

filterType=property-name
specifies the property by which to filter the resource list. Valid values for property-name are:


- For the namespaces and dataAccessPermissions resources, `name` and `tag`
- For the userAccounts resource, `username`

The filterType parameter is optional. For the namespaces and dataAccessPermissions resources, the default is `name`.
filterString=text-stringspecifies the text string to use as the filter. This string is not case sensitive.Text strings can be at most 64 characters long and can contain any valid UTF-8 characters except commas (,). White space is allowed and must be percent encoded.

For example, this GET request filters the list of namespaces for the Finance tenant by names beginning with the string accounts:

```
curl -k -i -H "Accept: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces
        ?filterType=name&filterString=accounts&prettyprint"
```

These considerations apply to filtering resource lists:

- The filterType and filterString parameters are valid only with the namespaces, userAccounts, and dataAccessPermissions resources.
- If a GET request includes the filterType parameter but does not include the filterString parameter, HCP returns the complete list of items of the applicable type.
- You can page, sort, and filter resource lists in the same request.

### Replication resources

Replication resources let you configure, monitor, and manage replication. The table below provides information about these resources.

## .../services/replication

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| replication<br>Service | GET | Retrieve Replication service settings | System-level user account with the monitor or administrator role |  |
| POST | Modify Replication service settings or perform an action on the Replication service | System-level user account with the administrator role |  |

## .../services/replication/certificates

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| Certificates | GET | Retrieves a list of replication certificates | System-level user account with the monitor or administrator role |  |
| N/A | PUT | Uploads a new replication certificate | System-level user account with the administrator role |  |

## .../services/replication/certificates/certificate-id

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| Text | GET | Retrieve information about a replication certificate | System-level user account with the monitor or administrator role |  |
| N/A | DELETE | Delete a replication certificate | System-level user account with the administrator role |  |

## .../services/replication/certificates/server

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| Text | GET | Downloads the replication server certificate and saves it on your computer as a `server_certificate.txt ` file | System-level user account with the administrator role |  |

## .../services/replication/links

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| link | PUT | Create a replication link | System-level user account with the administrator role |  |
| List | GET | Retrieve a list of the replication links in which the HCP system being queried participates | System-level user account with the monitor or administrator role | The listed replication links are identified by link name.<br>In XML, the element that identifies each link is **name**. The root element for the list of links is **links**.<br>In JSON, the name in the name/value pair that lists the links is **name**. |

## .../services/replication/links/link-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| link | GET | Retrieve information about a replication link | System-level user account with the monitor or administrator role |  |
| N/A | HEAD | Check for the existence of a replication link | System-level user account with the monitor or administrator role |  |
| link | POST | Modify or perform an action on a replication link | System-level user account with the administrator role |  |
| N/A | DELETE | Delete a replication link | System-level user account with the administrator role |  |

## .../services/replication/links/link-name/content

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| content | GET | Retrieve a list of the items included in a replication link | System-level user account with the monitor or administrator role |  |

## .../services/replication/links/link-name/content/defaultNamespaceDirectories

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the default-namespace directories included in a replication link | System-level user account with the monitor or administrator role | This resource is available only if the default tenant exists.<br>The listed directories are identified by directory name.<br>In XML, the element that identifies each directory is `name`. The root element for the list of directories is `defaultNamespace-Directories`.<br>In JSON, the name in the name/value pair that lists the directories is `name`. |

## .../services/replication/links/link-name/content/defaultNamespaceDirectories/directory-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| N/A | PUT | Add a default-namespace directory to a replication link | System-level user account with the administrator role |  |
| DELETE | Remove a default-namespace directory from a replication link | System-level user account with the administrator role |  |

## .../services/replication/links/link-name/content/chainedLinks

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the chained links included in an active/passive link | System-level user account with the monitor or administrator role | Not valid for active/active links.<br>The listed chained links are identified by link name.<br>In XML, the element that identifies each chained link is `name`. The root element of the list of chained links is `chainedLinks`.<br>In JSON, the name in the name/value pair that lists the chained links is `name`.<br>This resource is not valid for active/active links. |

## .../services/replication/links/link-name/content/chainedLinks/link-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| N/A | PUT | Add a chained link to an active/passive link | System-level user account with the administrator role | You cannot add a chained link to an active/active link. |
| DELETE | Remove a chained link from an active/passive link | System-level user account with the administrator role |  |

## .../services/replication/links/link-name/content/tenants

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the HCP tenants included in a replication link | System-level user account with the monitor or administrator role | The listed tenants are identified by tenant name.<br>In XML, the element that identifies each tenant is `name`. The root element for the list of tenants is `tenants`.<br>In JSON, the name in the name/value pair that lists the tenants is `name`. |

## .../services/replication/links/link-name/content/tenants/tenant-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| N/A | PUT | Add an HCP tenant to a replication link | System-level user account with the administrator role |  |
| tenant<br>(data type for replication link content tenant) | GET | Retrieve replication status information for a tenant included in a replication link | System-level user account with the monitor or administrator role |  |
| N/A | POST | Perform an action on a tenant included in a replication link | System-level user account with the administrator role |  |
| DELETE | Remove an HCP tenant from a replication link | System-level user account with the administrator role |  |

## .../services/replication/links/link-name/localCandidates

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| content | GET | Retrieve a list of the items on the system being queried that are eligible to be included in a replication link | System-level user account with the monitor or administrator role |  |

## .../services/replication/links/link-name/localCandidates/defaultNamespaceDirectories

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the default-namespace directories on the system being queried that are eligible to be included in a replication link | System-level user account with the monitor or administrator role | This resource is available only if the default tenant exists.<br>The listed directories are identified by directory name.<br>In XML, the element that identifies each directory is `name`. The root element for the list of directories is `defaultNamespace-Directories`.<br>In JSON, the name in the name/value pair that lists the directories is `name`. |

## .../services/replication/links/link-name/localCandidates/chainedLinks

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the inbound links on the system being queried that are eligible to be included in an active/passive link | System-level user account with the monitor or administrator role | Not valid for active/active links.<br>The listed chained links are identified by link name.<br>In XML, the element that identifies each chained link is `name`. The root element of the list of chained links is `chainedLinks`.<br>In JSON, the name in the name/value pair that lists the chained links is `name`.<br>This resource is not valid for active/active links. |

## .../services/replication/links/link-name/localCandidates/tenants

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the HCP tenants on the system being queried that are eligible to be included in a replication link | System-level user account with the monitor or administrator role | The listed tenants are identified by tenant name.<br>In XML, the element that identifies each tenant is `name`. The root element of the list of tenants is `tenants`.<br>In JSON, the name in the name/value pair that lists the tenants is `name`. |

## .../services/replication/links/link-name/remoteCandidates

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| content | GET | Retrieve a list of the items on the other system involved in a replication link that are eligible to be included in the link | System-level user account with the monitor or administrator role |  |

## .../services/replication/links/link-name/remoteCandidates/defaultNamespaceDirectories

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the default-namespace directories on the other system involved in a replication link that are eligible to be included in the link | System-level user account with the monitor or administrator role | This resource is available only if the default tenant exists.<br>The listed directories are identified by directory name.<br>In XML, the element that identifies each directory is `name`. The root element of the list of directories is `defaultNamespace-Directories`.<br>In JSON, the name in the name/value pair that lists the directories is `name`. |

## .../services/replication/links/link-name/remoteCandidates/chainedLinks

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the inbound links on the other system involved in an active/passive link that are eligible to be included in the link | System-level user account with the monitor or administrator role | Not valid for active/active links.<br>The listed chained links are identified by link name.<br>In XML, the element that identifies each chained link is `name`. The root element of the list of chained links is `chainedLinks`.<br>In JSON, the name in the name/value pair that lists the chained links is `name`.<br>This resource is not valid for active/active links. |

## .../services/replication/links/link-name/remoteCandidates/tenants

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the HCP tenants on the other system involved in a replication link that are eligible to be included in the link | System-level user account with the monitor or administrator role | The listed tenants are identified by directory name.<br>In XML, the element that identifies each tenant is `name`. The root element of the list of tenants is `tenants`.<br>In JSON, the name in the name/value pair that lists the tenants is `name`. |

## .../services/replication/links/link-name/schedule

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| schedule | GET | Retrieve the schedules for an active/active link or the schedule for an active/passive link | System-level user account with the monitor or administrator role |  |
| POST | Modify the schedules for an active/active link or the schedule for an active/passive link | System-level user account with the administrator role |  |

#### Example: Creating a replication link

Here’s a sample PUT request that creates an active/active link named MA-CA between the local HCP system, hcp-ma.example.com, and a remote HCP system named hcp-ca.example.com. The link definition is in an XML file named MA-CA.xml. The request is made using a system-level user account that includes the administrator role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<link>
    <name>MA-CA</name>
    <description>Active/active link between systems in MA and CA</description>
    <type>ACTIVE_ACTIVE</type>
    <connection>
         <remoteHost>replication.admin.hcp-ca.example.com</remoteHost>
    </connection>
    <compression>false</compression>
    <encryption>false</encryption>
    <priority>OLDEST_FIRST</priority>
    <failoverSettings>
         <local>
             <autoFailover>true</autoFailover>
             <autoFailoverMinutes>60</autoFailoverMinutes>
         </local>
         <remote>
             <autoFailover>true</autoFailover>
             <autoFailoverMinutes>60</autoFailoverMinutes>
         </remote>
    </failoverSettings>
</link>
```

## Request with cURL command line

```
curl -k -iT MA-CA.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp-ma.example.com:9090/mapi/services/replication/links"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("MA-CA.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
  "https://admin.hcp-ma.example.com:9090/mapi/services/" +
  "replication/links")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, os.path.getsize("MA-CA.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
PUT /mapi/services/replication/links HTTP/1.1
Host: admin.hcp-ma.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Content-Type: application/xml
Content-Length: 648
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Retrieving a replication certificate

Here's a sample GET request that retrieves a replication certificate from the HCP system. The request is made using a system-level user account that includes the administrator or monitor role.

## Request with cURL command line

```
curl -k -i -H "Accept: application/xml"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/services/replication/certificates
        ?prettyprint"
```

## Request in Python using PycURL

```
import pycurl
filehandle = open("certificates.xml", 'wb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Accept: application/xml",\
  "Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
  "https://admin.hcp.example.com:9090
  /mapi/services/replication/certificates")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.WRITEFUNCTION, filehandle.write)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
GET /mapi/services/replication/cerificates HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Accept: application/xml
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 329
```

## Response body

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<certificates>
    <certificate>
        <expiresOn>2021-12-12T15:35:10-0500</expiresOn>
        <id>server</id>
        <subjectDN>CN=*.example.com, OU=HCP, O=Hitachi, L=Waltham, ST=Massachusetts, C=US</subjectDN>
        <validOn>2016-12-12T15:35:10-0500</validOn>
    </certificate>
</certificates>
```

#### Example: Adding a replication certificate

Here's a sample PUT request that uploads a replication certificate to the HCP system. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -iT certificate.txt
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/services/replication/certificates"
```

## Request in Python using PycURL

```
import pycurl

filehandle = open("certificate.txt", 'wb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
  "https://admin.hcp.example.com:9090/mapi/" +
  "services/replication/certificates")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.CUSTOMREQUEST, "PUT")
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
PUT /mapi/services/replication/certificates HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Content-Type: application/xml
Content-Length: 1396
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
Expires: Thu, 01 Jan 1970 00:00:00 GMT
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Retrieving a list of eligible local candidates

Here’s a sample GET request that retrieves a list of the HCP tenants and default-namespace directories on the local system, hcp-ma.example.com, that are eligible to be included in the active/active replication link named MA-CA. The request writes the list of local candidates to a file named local-candidates.xml. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -i -H "Accept: application/xml"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp-ma.example.com:9090/mapi/services/replication/links/
         MA-CA/localCandidates?prettyprint" > local-candidates.xml
```

## Request in Python using PycURL

```
import pycurl
filehandle = open("local-candidates.xml", 'wb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Accept: application/xml",\
  "Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
  "https://admin.hcp-ma.example.com:9090/mapi/services/" +
  "replication/links/MA-CA/localCandidates?prettyprint")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.WRITEFUNCTION, filehandle.write)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
GET /mapi/services/replication/links/MA-CA/localCandidates?prettyprint HTTP/1.1
Host: admin.hcp-ma.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Accept: application/xml
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 330
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<content>
    <defaultNamespaceDirectories>
        <name>brochures_2015</name>
        <name>brochures_2016</name>
        <name>brochures_2017</name>
    </defaultNamespaceDirectories>
    <tenants>
        <name>Finance</name>
        <name>HR</name>
        <name>Sales-Mktg</name>
    </tenants>
</content>
```

#### Example: Adding an HCP tenant to a replication link

Here’s a sample PUT request that adds the tenant named Finance to the replication link named MA-CA. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp-ma.example.com:9090/mapi/services/replication/links/
         MA-CA/content/tenants/Finance"
```

## Request in Python using PycURL

```
import pycurl
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
  "https://admin.hcp-ma.example.com:9090/mapi/" +
  "services/replication/links/4-3/content/tenants/LisaTenant-2")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.CUSTOMREQUEST, "PUT")
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
```

## Request headers

```
PUT /mapi/services/replication/links/MA-CA/content/tenants/Finance HTTP/1.1
Host: admin.hcap-ma.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Setting the schedule for a replication link

Here’s the a sample POST request that sets the local and remote schedules for an active/active link named MA-CA. The new schedule is in an XML file named schedule\_MA-CA.xml. The request is made using a system-level user account that includes the administrator role.

## Request body in XML

```
<schedule>
    <local>
        <scheduleOverride>NONE</scheduleOverride>
        <transition>
            <time>Sun:00</time>
             <performanceLevel>HIGH</performanceLevel>
        </transition>
        <transition>
            <time>Sun:06</time>
            <performanceLevel>MEDIUM</performanceLevel>
        </transition>
        <transition>
             <time>Sat:00</time>
             <performanceLevel>HIGH</performanceLevel>
        </transition>
        <transition>
             <time>Sat:06</time>
             <performanceLevel>MEDIUM</performanceLevel>
        </transition>
    </local>
    <remote>
        <scheduleOverride>NONE</scheduleOverride>
        <transition>
             <time>Sun:00</time>
             <performanceLevel>HIGH</performanceLevel>
        </transition>
        <transition>
             <time>Mon:00</time>
             <performanceLevel>MEDIUM</performanceLevel>
        </transition>
    </remote>
</schedule>
```

## Request with cURL command line

```
curl -k -i -d @schedule_MA-CA.xml
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp-ma.example.com:9090/mapi/services/replication/links/
         MA-CA/schedule"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("schedule_MA-CA.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
  "https://admin.hcp-ma.example.com:9090/mapi/services/" +
  "replication/links/MA-CA/schedule")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.setopt(pycurl.INFILESIZE,
  os.path.getsize("schedule_MA-CA.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
POST /mapi/services/replication/links/MA-CA/schedule HTTP/1.1
Host: admin.hcp-ma.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Content-Type: application/xml
Content-Length: 807
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Failing over a replication link

Here’s a sample POST request that fails over the link named MA-CA to the local system, hcp-ca.example.com. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -iX POST
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp-ca.example.com:9090/mapi/services/replication/links/
         MA-CA?failOver"
```

## Request in Python using PycURL

```
import pycurl
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
  "https://admin.hcp-ca.example.com:9090/mapi/services/" +
  "replication/links/MA-CA?failOver")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
```

## Request headers

```
POST /mapi/services/replication/links/MA-CA?failOver HTTP/1.1
Host: admin.hcp-ca.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

### Retention class resources

Retention class resources let you create, retrieve information about, modify, and delete retention classes. The tables below provides information about these resources.

## .../tenants/tenant-name/namespaces/namespace-name/retentionClasses

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| retentionClass | PUT | Create a retention class for a namespace | - For an HCP namespace, tenant-level user account with the compliance role<br>- For the default namespace, system-level user account with the compliance role |  |
| List | GET | Retrieve a list of the retention classes defined for a namespace | - For an HCP namespace, tenant-level user account with the monitor, administrator, or compliance role<br>- For the default namespace, system-level user account with the monitor, administrator, or compliance role | The listed retention classes are identified by retention class name.<br>In XML, the element that identifies each retention class is `name`. The root element for the list of retention classes is `retentionClasses`.<br>In JSON, the name in the name/value pair that lists the retention classes is `name`. |

## .../tenants/tenant-name/namespaces/namespace-name/retentionClasses/retention-class-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| retentionClass | GET | Retrieve information about a retention class | - For an HCP namespace, tenant-level user account with the monitor, administrator, or compliance role<br>- For the default namespace, system-level user account with the monitor, administrator, or compliance role |  |
| N/A | HEAD | Check for the existence of a retention class | - For an HCP namespace, tenant-level user account with the monitor, administrator, or compliance role<br>- For the default namespace, system-level user account with the monitor, administrator, or compliance role |  |
| retentionClass | POST | Modify a retention class | - For an HCP namespace, tenant-level user account with the compliance role<br>- For the default namespace, system-level user account with the compliance role |  |
| N/A | DELETE | Delete a retention class | - For an HCP namespace, tenant-level user account with the compliance role<br>- For the default namespace, system-level user account with the compliance role | You can delete a retention class only if the namespace is in enterprise mode. |

#### Example: Creating a retention class

Here’s a sample PUT request that creates a retention class named FN-Std-42 for the Accounts-Receivable namespace. The retention class is defined in an XML file named RC-FN-Std-42.xml. The request is made using a tenant-level user account that includes the compliance role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<retentionClass>
    <name>FN-Std-42</name>
    <description>Implements Finance department standard #42 - keep for 10
         years.</description>
    <value>A+10y</value>
    <allowDisposition>true</allowDisposition>
</retentionClass>
```

## Request with cURL command line

```
curl -k -iT RC-FN-Std-42.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP bXdoaXRl:ad49ce36d0cec9634ef63b24151be0fb"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces/
         accounts-receivable/retentionClasses"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("RC-FN-Std-42.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  bXdoaXRl:ad49ce36d0cec9634ef63b24151be0fb"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
  "namespaces/accounts-receivable/retentionClasses")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, os.path.getsize("RC-FN-Std-42.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
PUT /mapi/tenants/finance/namespaces/accounts-receivable/retentionClasses
    HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bXdoaXRl:ad49ce36d0cec9634ef63b24151be0fb
Content-Type: application/xml
Content-Length: 275
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Retrieving a list of retention classes

Here’s a sample GET request that retrieves a list of the retention classes defined for the Accounts-Receivable namespace. The request writes the list of retention classes to a file named AR-retclasses.xml. The request is made using a tenant-level user account that includes the compliance role.

## Request with cURL command line

```
curl -k -H "Accept: application/xml"
    -H "Authorization: HCP bXdoaXRl:ad49ce36d0cec9634ef63b24151be0fb"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces/
         accounts-receivable/retentionClasses?prettyprint" > AR-retclasses.xml
```

## Request in Python using PycURL

```
import pycurl
filehandle = open("AR-retclasses.xml", 'wb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Accept: application/xml",\
  "Authorization: HCP \\
  bXdoaXRl:ad49ce36d0cec9634ef63b24151be0fb"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
  "namespaces/accounts-receivable/retentionClasses?prettyprint")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.WRITEFUNCTION, filehandle.write)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
GET /mapi/tenants/finance/namespaces/accounts-receivable/retentionClasses
    ?prettyprint HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bXdoaXRl:ad49ce36d0cec9634ef63b24151be0fb
Accept: application/xml
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 136
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<retentionClasses>
    <name>FN-Std-37</name>
    <name>FN-Std-42</name>
</retentionClasses>
```

### Service statistics resource

The service statistics resource lets you retrieve the statistical information of services used by your HCP system.

Note: A best practice is to limit your HTTP GET requests for retrieving statistics and metrics to once per hour. Polling the system more frequently can lead to system instability.


## .../services/statistics

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| serviceStatistics | GET | Retrieve the statistics of services used by the HCP system | System-level user with the administrator or monitor role. |  |

#### Example: Retrieving service statistics

Here is a sample request that retrieves service statistics for a system. In this example, statistics are collected on a service that is running, a service that is ready to run, and a service that is currently disabled.

## Request with cURL command line

```
curl -ik -H "Authorization: HCP ZGVa3b9c17d52107f34fhdb83c7a5" -H "Accept: application/xml" "https://admin.hcp.example.com:9090/mapi/services/statistics?prettyprint"
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<serviceStatistics>
     <requestTime>1524773822369</requestTime>
     <services>
          <service>
               <name>StorageTieringService</name>
               <state>RUNNING</state>
               <startTime>1564040581</startTime>
               <formattedStartTime>7/25/2019 3:43:01 EDT</formattedStartTime>
               <endTime>-1</endTime>
               <performanceLevel>HIGH</performanceLevel>
               <objectsExamined>1056</objectsExamined>
               <objectsExaminedPerSecond>1056.0</objectsExaminedPerSecond>
               <objectsServiced>0</objectsServiced>
               <objectsServicedPerSecond>0.0</objectsServicedPerSecond>
               <objectsUnableToService>0</objectsUnableToService>
               <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>
          </service>
          <service>
               <name>GarbageCollection</name>
               <state>READY</state>
               <startTime>1539531912</startTime>
               <formattedStartTime>7/25/2019 0:00:00 EST</formattedStartTime>
               <endTime>1539576002</endTime>
               <formattedEndTime>7/25/2019 0:04:49 EST</formattedEndTime>
               <objectsExamined>29530</objectsExamined>
               <objectsExaminedPerSecond>1102.05</objectsExaminedPerSecond>
               <objectsServiced>27570</objectsServiced>
               <objectsServicedPerSecond>.08</objectsServicedPerSecond>
               <objectsUnableToService>0</objectsUnableToService>
               <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>
          </service>
          <service>
               <name>RetirementPolicy</name>
               <state>DISABLED</state>
               <startTime>-1</startTime>
               <formattedStartTime>-1</formattedStartTime>
               <endTime>-1</endTime>
               <formattedEndTime>-1</formattedEndTime>
               <objectsExamined>0</objectsExamined>
               <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>
               <objectsServiced>0</objectsServiced>
               <objectsServicedPerSecond>0.0</objectsServicedPerSecond>
               <objectsUnableToService>0</objectsUnableToService>
               <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>
          </service>
     </services>
<serviceStatistics>
```

### Support access credentials resource

Use the Support access credentials resource to retrieve current Hitachi Vantara Support access credentials and to configure exclusive Support access credentials for additional security compliance.

Note:

- To retrieve Support access credentials for an HCP system, a system-level user account with the monitor, administrator, security, or service role is required.
- To configure exclusive Hitachi Vantara Support access credentials for an HCP system, a system-level user account with the administrator or service role is required.

## .../supportaccesscredentials

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| supportAccessCredentials | GET | Retrieve the currently configured Support access credentials. | System-level user with the monitor,administrator, security, or service role. |  |
|  | PUT | Configure exclusive Support access credentials by uploading the exclusive Hitachi Vantara Support access credentials package to the HCP system. | System-level user with the administrator or service role. |  |

## Example: Retrieving information about Support access credentials when exclusive Support access credentials are installed

Here is a sample GET request that retrieves information about the currently configured exclusive Hitachi Vantara Support access credentials on the HCP system.

Request using the cURL command line:

```
curl -i -k  -b hcp-api-auth="c2VjdXJpdHk=:a3b9c163f6c520407ff34cfd"
 https://172.18.150.236:9090/mapi/supportaccesscredentials?prettyprint
```

Response body:

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
     <supportAccessCredentials>
          <applyTimeStamp>1599143327</applyTimeStamp>
          <createTimeStamp>1597699675</createTimeStamp>
          <type>Exclusive</type>
          <defaultKeyType>Arizona</defaultKeyType>
          <serialNumberFromPackage>425999</serialNumberFromPackage>
     </supportAccessCredentials>
```

## Example: Retrieving information about Support access credentials when the default Support access credentials are installed

Here is a sample GET request that retrieves information about the currently configured default Hitachi Vantara Support access credentials.

Request using the cURL command line:

```
curl -i -k  -b hcp-api-auth="c2VjdXJpdHk=:a3b9c163f6c520407ff34cfd"
 https://172.18.150.236:9090/mapi/supportaccesscredentials?prettyprint
```

Response body:

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
     <supportAccessCredentials>
          <type>Default</type>
          <defaultKeyType>Arizona</defaultKeyType>
     </supportAccessCredentials>
```

## Example: Updating Support access credentials to exclusive Hitachi Vantara Support access

Here is a sample PUT request that updates the Support access credentials on an HCP system to exclusive Hitachi Vantara Support access by uploading an SSH key from the Exclusive Hitachi Vantara Support Access Credentials package.

Request using the cURL command line:

```
curl -i -k -T HCP-SSHKeyPackage-04022020_14-58-20.plk
 -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
 https://admin.hcp.example.com:9090/mapi/supportAccesscredentials
```

Response header:

```
Response header:HTTP/1.1 200 OK
Content-Type: application/xml
Expires: Thu, 07 Oct 2021 00:00:00 GMT
X-HCP-SoftwareVersion: 9.2.0.251
Content-Length: 0
```

### System-level group account resources

System-level group account resources let you retrieve information about the system-level group accounts. The tables below provide information about these resources.

## .../groupAccounts

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the system-level group accounts. | System-level user account with the monitor, administrator, or security role | The listed group accounts are identified by the group name.<br>In XML, the element that identifies each group account is `groupname`. The root element for the list of group accounts is `groupAccounts`.<br>In JSON, the name in the name/value pair that lists the group accounts is `groupname`. |

## .../groupAccounts/group-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| groupAccount | GET | Retrieve information about a group account. | System-level user account with the monitor, administrator, or security role | The information returned depends on the roles associated with the user making the request. |
| N/A | HEAD | Check for the existence of a group account. | System-level user account with the monitor, administrator, or security role |  |

#### Example: Retrieving system-level group accounts

Here is a sample GET request that retrieves a list of system-level group accounts. The request writes the list of group accounts to a file named systemLevelGroups.xml. The request is made with a system-level user account that includes the monitor role.

## Request with cURL command line

```
curl -k -i -H "Authorization: bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp.example.com:9090/mapi/groupAccounts?verbose=true&
     prettyprint" >systemLevelGroups.xml
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<groupAccounts>
    <groupname>Admins@dev.hcp.example.com</groupname>
    <groupname>Admins@finance.hcp.example.com</groupname>
    <groupname>Admins@sales.hcp.example.com</groupname>
</groupAccounts>
```

#### Example: Retrieving system-level group account information

Here is a sample GET request that retrieves the system-level group account information for the groupname Admins@dev.hcp.example.com. The request writes the group account information to a file named systemLevelGroup-Admins-dev-hcp-ex.xml. The request is made with a system-level user account that includes the monitor role.

## Request with cURL command line

```
curl -k -i -H "Authorization: bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp.example.com:9090/mapi/groupAccounts/
    Admins@dev.hcp.example.com?verbose=true&prettyprint" >
    systemLevelGroup-Admins-dev-hcp-ex.xml
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<groupAccount>
    <allowNamespaceManagement>false</allowNamespaceManagement>
    <externalGroupID>S-1-2-34-123456789-1234567890-1234567890-
       1234<externalGroupID>
    <groupname>Admins@dev.hcp.example.com</groupname>
    <roles>
        <role>SEARCH</role>
        <role>MONITOR</role>
        <role>SERVICE</role>
        <role>COMPLIANCE</role>
    <roles>
</groupAccount>
```

### System-level user account resources

The system-level user account resources let you retrieve a list of the system-level user accounts, retrieve information about a system-level user account, and change the password for a locally authenticated system-level user account. The table below provides information about these resources.

## .../userAccounts

| Data Type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the system-level user accounts. | System-level user account with the monitor, administrator, or security role | The listed user accounts are identified by the account username.<br>In XML, the element that identifies each user account is `username`. The root element for the list of user accounts is `userAccounts`.<br>In JSON, the name in the name/value pair that lists the user accounts is `username`. |

## .../userAccounts/username

| Data Type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| userAccount | GET | Retrieve information about a system-level user account. | System-level user account with the monitor, administrator, or security role |  |
| N/A | HEAD | Check for the existence of a user account. | System-level user account with the monitor, administrator, or security role |  |
| N/A | POST | Change the password for a locally authenticated system-level user account. | System-level user account with the security role | The required query parameter for changing user account passwords is `password=password`. |

## .../userAccounts/username/changePassword

| Data Type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| updatePasswordRequest | POST | Change the password for a locally authenticated system-level user account. | System-level user account with the security role |  |

#### Example: Retrieving system-level user accounts

Here is a sample GET request that retrieves a list of system-level user accounts. The request writes the list of user accounts to a file named systemLevelUsers.xml. The request is made with a system-level user account that includes the monitor role.

## Request with cURL command line

```
curl -k -i -H "Authorization: bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp.example.com:9090/mapi/userAccounts?verbose=true&
     prettyprint" > systemLevelUsers.xml
```

## Response body

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<userAccounts>
    <username>lgreen</username>
    <username>mwhite</username>
    <username>pblack</username>
</userAccounts>
```

#### Example: Retrieving system-level user account information

Here is a sample GET request that retrieves the system-level user account information for the username lgreen. The request writes the information to a file named systemLevelUser-lgreen.xml. The request is made with a system-level user account that includes the monitor role.

## Request with cURL command line

```
curl -k -i -H "Authorization: bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp.example.com:9090/mapi/userAccounts/lgreen?verbose=true&
     prettyprint" >systemLevelUser-lgreen.xml
```

## Response body

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<userAccount>
    <enabled>true</enabled>
    <localAuthentication>true</localAuthentication>
    <roles>
        <role>ADMINISTRATOR</role>
        <role>SEARCH</role>
        <role>MONITOR</role>
        <role>SECURITY</role>
        <role>SERVICE</role>
        <role>COMPLIANCE</role>
    </roles>
    <allowNamespaceManagement>true</allowNamespaceManagement>
    <description>Developer, Team Lead</description>
    <forcePasswordChange>false</forcePasswordChange>
    <fullName>Lee Green</fullName>
    <userGUID>eaa046e0-c17e-42fb-8840-ab5e05cf8876</userGUID>
    <userID>104</userID>
    <username>lgreen</username>
</userAccount>
```

#### Example: Changing a user account password with a query parameter

Here’s a sample POST request that uses the password query parameter to change the password for the system-level user account with the username lgreen to End321!. The request is made using a system-level user account that includes the security role.

## Request with cURL command line

```
curl -k -i -d "{}" -H "Content-Type: application/xml"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/userAccounts/lgreen
        ?password=End321!"
```

## Request in Python using PycURL

```
import pycurl
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
 "https://admin.hcp.example.com:9090/mapi/" +
 "userAccounts/lgreen?password=End321!")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
```

## Request headers

```
POST /mapi/userAccounts/?password=End321! HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Content-Type: application/xml
Content-Length: 2
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Changing a user account password in the request body

Here’s a sample POST request that uses the `updatePasswordRequest` data type in the request body to change the password for the system-level user account with the username lgreen. The requested new password is End321!, which is specified in an XML file named Password.xml. The request is made using a system-level user account that includes the security role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<updatePasswordRequest>
    <newPassword>End321!</newPassword>
</updatePasswordRequest>
```

## Request with cURL command line

```
curl -k -i -d @Password.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/userAccounts/lgreen
        /changePassword"
```

## Request headers

```
POST /mapi/userAccounts/lgreen/changePassword HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Content-Type: application/xml
Content-Length: 2
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

### Tenant resources

Tenant resources let you create, retrieve information about, modify, and delete tenants. The tables below provides information about these resources.

## .../tenants/tenant-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| tenant | GET | Retrieve information about a tenant | - For an HCP tenant, tenant-level user account with the monitor or administrator role<br>- For the default tenant, system-level user account with the monitor or administrator role |  |
| N/A | HEAD | Check for the existence of a tenant | - For an HCP tenant, tenant-level user account with the monitor or administrator role<br>- For the default tenant, system-level user account with the monitor or administrator role |  |
| tenant | POST | Modify a tenant | - For an HCP tenant tenant-level user account with the administrator role<br>- For the default tenant, system-level user account with the administrator role |  |

## .../tenants/tenant-name/availableServicePlans

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve a list of the service plans that are available for the tenant to assign to its namespaces | - For an HCP tenant, tenant-level user account with the monitor or administrator role<br>- For the default tenant, system-level user account with the monitor or administrator role | Valid only if the tenant is configured to allow service plan selection.<br>The listed service plans are identified by service plan name.<br>In XML, each listed service plan is the value of an element named `name`. In JSON, the name in the name/value pair that lists the service plans is `name`. |

## .../tenants/tenant-name/availableServicePlans/service-plan-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| available ServicePlan | GET | Retrieve information about a service plan that’s available for the tenant to assign to its namespaces | - For an HCP tenant, tenant-level user account with the monitor or administrator role<br>- For the default tenant, system-level user account with the monitor or administrator role | Valid only if the tenant is configured to allow service plan selection. |

## .../tenants/tenant-name/chargebackReport

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| chargeback Report | GET | Generate a chargeback report for a tenant | Tenant-level user account with the monitor or administrator role | Not valid for the default tenant.<br>Supported output formats are XML, JSON, and CSV. |

## .../tenants/tenant-name/consoleSecurity

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| consoleSecurity | GET | Retrieve the Tenant Management Console configuration for a tenant | Tenant-level user account with the security role | Not valid for the default tenant. |
| POST | Modify the Tenant Management Console configuration for a tenant | Tenant-level user account with the security role |

## .../tenants/tenant-name/contactInfo

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| contactInfo | GET | Retrieve the contact information for a tenant | - For an HCP tenant, tenant-level user account with the monitor or administrator role<br>- For the default tenant, system-level user account with the monitor or administrator role |  |
| POST | Modify the contact information for a tenant | - For an HCP tenant, tenant-level user account with the administrator role<br>- For the default tenant, system-level user account with the administrator role |  |

## .../tenants/tenant-name/cors

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| cors | PUT | Set the default CORS rules configuration for all namespaces owned by the tenant | - System-level user account with the administrator role<br>- Tenant-level user account with the administrator role | If the default tenant-level CORS configuration is set, it is applicable for all namespaces that do not have a namespace-level CORS configuration. |
| GET | Retrieve the default CORS configuration for all namespaces owned by the tenant | - System-level user account with the monitor or administrator role<br>- Tenant-level user account with the monitor or administrator role |  |
| DELETE | Delete the default CORS configuration for all namespaces owned by the tenant | - System-level user account with the administrator role<br>- Tenant-level user account with the administrator role | If the resource does not have a default CORS configuration, the HTTP status code `404 Not Found` is returned. |

## .../tenants/tenant-name/emailNotification

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| email Notification | GET | Retrieve the email notification configuration for a tenant | - For an HCP tenant, tenant-level user account with the monitor or administrator role<br>- For the default tenant, system-level user account with the with the monitor or administrator role |  |
| POST | Modify the email notification configuration for a tenant | - For an HCP tenant, tenant-level user account with the administrator role<br>- For the default tenant, system-level user account with the administrator role |  |

## .../tenants/tenant-name/namespaceDefaults

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| namespace Defaults | GET | Retrieve the default settings for namespace creation for a tenant | Tenant-level user account with the monitor or administrator role | Not valid for the default tenant. |
| POST | Modify the default settings for namespace creation for a tenant | Tenant-level user account with the administrator role |

## .../tenants/tenant-name/permissions

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| List | GET | Retrieve the list of permissions in the data access permission mask for a tenant | - For an HCP tenant, tenant-level user account with the monitor, administrator, or compliance role<br>- For the default tenant, system-level user account with the monitor, administrator, or compliance role |  |
| List | POST | Modify the list of permissions in the data access permission mask for a tenant | - For an HCP tenant, tenant-level user account with the administrator role<br>- For the default tenant, system-level user account with the administrator role | Valid values for permissions are:<br>- DELETE<br>- PRIVILEGED<br>- PURGE<br>- READ<br>- SEARCH<br>- WRITE<br>These values are case sensitive.<br>The set of permissions specified in the request body replaces the set of permissions currently included in the data access permission mask for the tenant. To remove all permissions, specify an empty list.<br>If the set of permissions includes PURGE, delete permission is enabled automatically. If the set of permissions includes SEARCH, read permission is enabled automatically. |

## .../tenants/tenant-name/searchSecurity

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| searchSecurity | GET | Retrieve the Search Console configuration for a tenant | Tenant-level user account with the security role | Not valid for the default tenant. |
| searchSecurity | POST | Modify the Search Console configuration for a tenant | Tenant-level user account with the security role | Not valid for the default tenant. |

## .../tenants/tenant-name/statistics

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| statistics | GET | Retrieve statistics about the content of the namespaces owned by a tenant | - For an HCP tenant, tenant-level user account with the monitor or administrator role<br>- For the default tenant, system-level user account with the monitor or administrator role |  |

#### Example: Creating an HCP tenant

Here’s a sample PUT request that creates a tenant named Finance in the HCP system named hcp.example.com. The tenant definition is in an XML file named FinanceTenant.xml. The initial user account for the tenant has a username of lgreen and a password of start123. These are specified by query parameters. The request is made using a system-level user account that includes the administrator role.

This example assumes the existence of a service plan named Short-Term-Activity.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<tenant>
    <name>Finance</name>
    <systemVisibleDescription>Created for the Finance department at Example
        Company by P.D. Grey on 2/9/2017.</systemVisibleDescription>
    <hardQuota>100 GB</hardQuota>
    <softQuota>90</softQuota>
    <namespaceQuota>5</namespaceQuota>
    <authenticationTypes>
        <authenticationType>LOCAL</authenticationType>
        <authenticationType>EXTERNAL</authenticationType>
    </authenticationTypes>
    <complianceConfigurationEnabled>true</complianceConfigurationEnabled>
    <versioningConfigurationEnabled>true</versioningConfigurationEnabled>
    <searchConfigurationEnabled>true</searchConfigurationEnabled>
    <replicationConfigurationEnabled>true</replicationConfigurationEnabled>
    <erasureCodingSelectionEnabled>true</erasureCodingSelectionEnabled>
    <tags>
        <tag>Example Company</tag>
        <tag>pdgrey</tag>
    </tags>
    <servicePlanSelectionEnabled>false</servicePlanSelectionEnabled>
    <servicePlan>Short-Term-Activity</servicePlan>
    <dataNetwork>net127</dataNetwork>
    <managementNetwork>net004</managementNetwork>
</tenant>
```

## Request with cURL command line

```
curl -k -iT FinanceTenant.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/tenants?username=lgreen
        &password=start123&forcePasswordChange=false"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("FinanceTenant.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
  "https://admin.hcp.example.com:9090/mapi/tenants?" +
  "username=lgreen&password=start123&forcePasswordChange=false")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, os.path.getsize("FinanceTenant.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
PUT /mapi/tenants?username=lgreen&password=start123
    &forcePasswordChange=false HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Content-Type: application/xml
Content-Length: 1016
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Setting Tenant Management Console security for a tenant

Here’s a sample POST request that modifies the Tenant Management Console configuration for the Finance tenant. The Console configuration information is in an XML file named FinanceMgmtConsole.xml. The request is made using a tenant-level user account that includes the security role.

## Request body in XML

```
<consoleSecurity>
<automaticUserAccountUnlockSetting>false</automaticUserAccountUnlockSetting>
<automaticUserAccoutUnlockDuration>0</automaticUserAccoutUnlockDuration>
<blockCommonPassword>false</blockCommonPassword>
<blockPasswordReUse>false</blockPasswordReUse>
<coolDownPeriodDuration>5</coolDownPeriodDuration>
<coolDownPeriodSettings>false</coolDownPeriodSettings>
<disableAfterAttempts>5</disableAfterAttempts>
<disableAfterInactiveDays>180</disableAfterInactiveDays>
<forcePasswordChangeDays>180</forcePasswordChangeDays>
    <ipSettings>
	<allowAddresses>
		<ipAddress>192.168.103.18</ipAddress>
             <ipAddress>192.168.103.24</ipAddress>
             <ipAddress>192.168.103.25</ipAddress>
	</allowAddresses>
	<lowlfInBothLists>false</lowlfInBothLists>
	<denyAddresses/>
    </ipSettings>
<loginMessage> </loginMessage>
<logoutOnInactive>10</logoutOnInactive>
<lowerCaseLetterCount>0</lowerCaseLetterCount>
<minimumPasswordLength>6</minimumPasswordLength>
<numericCharacterCount>0</numericCharacterCount>
<passwordcombination>false</passwordcombination>
<passwordContainsUsername>true</passwordContainsUsername>
<passwordReuseDepth>4</passwordReuseDepth>
<specialCharacterCount>0</specialCharacterCount>
<upperCaseLetterCount>0</upperCaseLetterCount>
</consoleSecurity>
```

## Request with cURL command line

```
curl -k -i -d @FinanceMgmtConsole.xml
    -H "Content-Type: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/consoleSecurity"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("FinanceMgmtConsole.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
  "consoleSecurity")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.setopt(pycurl.INFILESIZE,
  os.path.getsize("FinanceMgmtConsole.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
POST /mapi/tenants/finance/consoleSecurity HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6
Content-Type: application/xml
Content-Length: 620
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Generating a chargeback report for a tenant

Here’s a sample GET request that generates a chargeback report for the tenant named europe that has two namespaces, finance and hr. The report covers one day. The report is written in CSV format to a file named FinanceStats-2020-01-31.txt. The request is made using a tenant-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -i -H "Accept: text/csv"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/
         chargebackReport?start=2020-01-31T00:00:00-0500
         &end=2020-01-31T13:59:59-0500&granularity=day"
    > FinanceStats-2020-01-31.txt
```

## Request in Python using PycURL

```
import pycurl
filehandle = open("FinanceStats-2020-01-31.txt", 'wb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Accept: text/csv",\
  "Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
  "chargebackReport?start=2020-01-31T00:00:00-0500" +
  "&end=2020-01-31T13:59:59-0500&granularity=day")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.WRITEFUNCTION, filehandle.write)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
GET /mapi/tenants/finance/chargebackReport?start=2017-02-17T00:00:00-0500&end=2020-01-31T23:59:59-0500&granularity=day HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6
Accept: text/csv
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: text/csv
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 767
```

## Response body

```
systemName,tenantName,namespaceName,startTime,endTime,objectCount,ingestedVolume,storageCapacityUsed,bytesIn,bytesOut,reads,writes,deletes,multipartObjects,multipartObjectParts,multipartObjectBytes,multipartUploads,multipartUploadParts,multipartUploadBytes,deleted,valid
finance.hcp.example.com,europe,finance,2017-02-17T00:00:00-0500,2017-02-17T23:59:59-0500,6,134243721,134270976,123986263,87561,1,10,0,2,7,93213889,0,0,0,false,true
finance.hcp.example.com,europe,hr,2017-02-17T00:00:00-0500,2017-02-17T23:59:59-0500,7,9609368,9621504,9609368,0,0,7,0,0,0,0,0,0,0,false,true
finance.hcp.example.com,europe,,2017-02-17T00:00:00-0500,2017-02-17T23:59:59-0500,13,143853089,143892480,133595631,87561,1,17,0,2,7,93213889,0,0,0,false,true
```

### Tenant-level group account resources

Tenant-level group account resources let you create, retrieve information about, modify, and delete tenant-level group accounts. The table below provides information about these resources.

Group account resources are not available for the default tenant.

## .../tenants/tenant-name/groupAccounts

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| groupAccount | PUT | Create a group account for a tenant | Tenant-level user account with the security role | You can create a group account only if HCP is configured to support AD. |
| List | GET | Retrieve a list of the group accounts defined for a tenant | Tenant-level user account with the monitor, administrator, or security role | The listed group accounts are identified by the group name.<br>In XML, the element that identifies each group account is `groupname`. The root element for the list of group accounts is `groupAccounts`.<br>In JSON, the name in the name/value pair that lists the group accounts is `groupname`. |

## .../tenants/tenant-name/groupAccounts/group-name

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| groupAccount | GET | Retrieve information about a group account | Tenant-level user account with the monitor, administrator, or security role | The information returned depends on the roles associated with the user making the request. |
| N/A | HEAD | Check for the existence of a group account | Tenant-level user account with the monitor, administrator, or security role |  |
| groupAccount | POST | Modify a group account | Tenant-level user account with the administrator or security role | A user with only the administrator role can modify only the `allow-NamespaceManagement` property. A user with only the security role cannot modify that property. |
| N/A | DELETE | Delete a group account | Tenant-level user account with the security role |  |

## .../tenants/tenant-name/groupAccounts/group-name/dataAccessPermissions

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| dataAccess<br>Permissions | GET | Retrieve information about the data access permissions associated with a group account | Tenant-level user account with the administrator, security, or monitor role |  |
| POST | Modify the data access permissions associated with a group account | Tenant-level user account with the administrator role | The request body must contain all permissions granted for each included namespace. If a namespace is not included, its permissions are not changed by the POST request.<br>By default, when you create a group account, it does not include any data access permissions. |

#### Example: Creating a group account

Here’s a sample PUT request that creates a group account for the Finance tenant. The account is defined in an XML file named admin-GA.xml. The name of the group account is specified in the XML file. For the request to be successful, a group with this name must already exist in AD. The request is made using a tenant-level user account that includes the security role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<groupAccount>
    <groupname>hcp-admin@ad.example.com</groupname>
    <roles>
         <role>MONITOR</role>
         <role>ADMINISTRATOR</role>
    </roles>
</groupAccount>
```

## Request with cURL command line

```
curl -k -iT admin-GA.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/groupAccounts"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("admin-GA.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
  "groupAccounts")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, os.path.getsize("admin-GA.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
PUT /mapi/tenants/finance/groupAccounts HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6
Content-Type: application/xml
Content-Length: 365
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example Retrieving group accounts

Here’s a sample GET request that retrieves all the group accounts defined for the Finance tenant. The request writes the list of group accounts to a file named finance-groups.xml. The request is made using a tenant-level user account that includes the security role.

## Request with cURL command line

```
curl -k -H "Accept: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/groupAccounts
         ?prettyprint" > finance-groups.xml
```

## Request in Python using PycURL

```
import pycurl
filehandle = open("finance-groups.xml", 'wb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Accept: application/xml",\
  "Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
  "groupAccounts?prettyprint")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.WRITEFUNCTION, filehandle.write)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
GET /mapi/tenants/groupAccounts?prettyprint HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6
Accept: application/xml
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 307
```

## Response body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<groupAccounts>
    <groupname>hcp-admin@ad.example.com</groupname>
    <groupname>hcp-security@ad.example.com</groupname>
    <groupname>hcp-compliance@ad.example.com</groupname>
    <groupname>AR-read@ad.example.com</groupname>
    <groupname>AR-full-access@ad.example.com</groupname>
    <groupname>AP-read@ad.example.com</groupname>
    <groupname>AP-full-access@ad.example.com</groupname>
</groupAccounts>
```

### Tenant-level user account resources

Tenant-level user account resources let you create, retrieve information about, modify, and delete tenant-level user accounts. The table below provides information about these resources.

User account resources are not available for the default tenant.

## .../tenants/tenant-name/userAccounts

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| userAccount | PUT | Create a user account for a tenant | Tenant-level user account with the security role |  |
| List | GET | Retrieve a list of the user accounts defined for a tenant | Tenant-level user account with the monitor, administrator, or security role | The listed user accounts are identified by the account username.<br>In XML, the element that identifies each user account is `username`. The root element for the list of user accounts is `userAccounts`.<br>In JSON, the name in the name/value pair that lists the user accounts is `username`. |

## .../tenants/tenant-name/userAccounts/username

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| userAccount | GET | Retrieve information about a user account | Tenant-level user account with the monitor, administrator, or security role | The information returned depends on the roles associated with the user making the request. |
| N/A | HEAD | Check for the existence of a user account | Tenant-level user account with the monitor, administrator, or security role |  |
| userAccount | POST | Modify a user account | Tenant-level user account with the administrator or security role | A user with only the administrator role can modify only the `allow-NamespaceManagement` property. A user with only the security role cannot modify that property. |
| N/A | DELETE | Delete a user account | Tenant-level user account with the security role |  |

## .../tenants/tenant-name/userAccounts/username/changePassword

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| updatePassword Request | POST | Change the password for a locally authenticated tenant-level user account | System-level user account with the security role or tenant-level user account with the security role |  |

## .../tenants/tenant-name/userAccounts/username/dataAccessPermissions

| Data type | Method | Use | Access | Notes |
| --- | --- | --- | --- | --- |
| dataAccess Permissions | GET | Retrieve information about the data access permissions associated with a user account | Tenant-level user account with the administrator or security role |  |
| POST | Modify the data access permissions associated with a user account | Tenant-level user account with the administrator role | The request body must contain all permissions granted for each included namespace. If a namespace is not included, its permissions are not changed by the POST request.<br>By default, when you create a user account, it does not include any data access permissions. |

#### Example: Creating a user account

Here’s a sample PUT request that creates a user account for the Finance tenant. The account is defined in an XML file named mwhite-UA.xml. The username for the account is specified in the XML file. The password is specified by the `password` query parameter. The request is made using a tenant-level user account that includes the security role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<userAccount>
    <username>mwhite</username>
    <fullName>Morgan White</fullName>
    <description>Compliance officer.</description>
    <localAuthentication>true</localAuthentication>
    <forcePasswordChange>true</forcePasswordChange>
    <enabled>true</enabled>
    <roles>
         <role>COMPLIANCE</role>
         <role>MONITOR</role>
    </roles>
    <allowNamespaceManagement>false</allowNamespaceManagement>
</userAccount>
```

## Request with cURL command line

```
curl -k -iT mwhite-UA.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/userAccounts
         ?password=start123"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("mwhite-UA.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
  "userAccounts?password=start123")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.INFILESIZE, os.path.getsize("mwhite-UA.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
PUT /mapi/tenants/finance/userAccounts?password=start123 HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6
Content-Type: application/xml
Content-Length: 365
```

## Response headers

```
HTTP/1.1 200 OK
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Changing the roles associated with a user account

Here’s a sample POST request that changes the roles associated with the user account with the username mwhite. The new set of roles is specified in an XML file named mwhite-UAroles.xml. The request is made using a tenant-level user account that includes the security role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<userAccount>
    <roles>
         <role>COMPLIANCE</role>
         <role>MONITOR</role>
         <role>ADMINISTRATOR</role>
    </roles>
</userAccount>
```

## Request with cURL command line

```
curl -k -i -d @mwhite-UAroles.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/
         userAccounts/mwhite"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("mwhite-UAroles.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
  "userAccounts/mwhite")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.setopt(pycurl.INFILESIZE,
  os.path.getsize("mwhite-UAroles.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
POST /mapi/tenants/finance/userAccounts/mwhite HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6
Content-Type: application/xml
Content-Length: 120
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Changing the data access permissions associated with a user account

Here’s a sample POST request that changes the data access permissions associated with the user account with the username pblack. The new set of permissions is specified in an XML file named pblack-UAperms.xml. The file includes permissions for both the Accounts-Receivable and Accounts-Payable namespaces. The request is made using a tenant-level user account that includes the administrator role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<dataAccessPermissions>
    <namespacePermission>
         <namespaceName>Accounts-Receivable</namespaceName>
         <permissions>
             <permission>READ</permission>
              <permission>BROWSE</permission>
              <permission>WRITE</permission>
              <permission>DELETE</permission>
              <permission>PURGE</permission>
              <permission>SEARCH</permission>
          </permissions>
     </namespacePermission>
    <namespacePermission>
         <namespaceName>Accounts-Payable</namespaceName>
       <permissions>
             <permission>READ</permission>
         </permissions>
    </namespacePermission>
</dataAccessPermissions>
```

## Request with cURL command line

```
curl -k -i -d @pblack-UAperms.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/userAccounts/
         pblack/dataAccessPermissions"
```

## Request in Python using PycURL

```
import pycurl
import os
filehandle = open("pblack-UAperms.xml", 'rb')
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Content-Type: application/xml",\
  "Authorization: HCP \\
  bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"])
curl.setopt(pycurl.URL,
  "https://finance.hcp.example.com:9090/mapi/tenants/finance/" +
  "userAccounts/pblack/dataAccessPermissions")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.UPLOAD, 1)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.setopt(pycurl.INFILESIZE,
   os.path.getsize("pblack-UAperms.xml"))
curl.setopt(pycurl.READFUNCTION, filehandle.read)
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
filehandle.close()
```

## Request headers

```
POST /mapi/tenants/finance/userAccounts/pblack/dataAccessPermissions
    HTTP/1.1
Host: finance.hcp.example.com:9090
Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6
Content-Type: application/xml
Content-Length: 572
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Changing a user account password in the request body

Here’s a sample POST request that uses the `updatePasswordRequest` data type in the request body to change the password for a Finance tenant user account with the username lgreen . The requested new password is End321!, which is specified in an XML file named Password.xml. The request is made using a system-level user account that includes the security role.

## Request body in XML

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<updatePasswordRequest>
    <newPassword>End321!</newPassword>
</updatePasswordRequest>
```

## Request with cURL command line

```
curl -k -i -d @Password.xml -H "Content-Type: application/xml"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/tenants/finance
        /userAccounts/lgreen/changePassword"
```

## Request headers

```
POST /mapi/tenants/finance/userAccounts/lgreen/changePassword HTTP/1.1
Host: admin.hcp.example.com:9090
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Content-Type: application/xml
Content-Length: 2
```

## Response headers

```
HTTP/1.1 200 OK
Content-Type: application/xml
X-HCP-SoftwareVersion: 9.0.0.2
Content-Length: 0
```

#### Example: Resetting the security user passwords for a tenant

Here’s a sample POST request that resets the passwords for the tenant named Finance to start123. The empty request body has a content type of JSON. The request is made using a system-level user account that includes the administrator role.

## Request with cURL command line

```
curl -k -i -d "{}" -H "Content-Type: application/json"
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"
    "https://admin.hcp.example.com:9090/mapi/tenants/finance/userAccounts
        ?resetPasswords=start123"
```

## Request in Python using PycURL

```
import pycurl
curl = pycurl.Curl()
curl.setopt(pycurl.HTTPHEADER, ["Authorization: HCP \\
    YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"])
curl.setopt(pycurl.URL,
    "https://admin.hcp-ca.example.com:9090/mapi/tenants/finance/" +
    "userAccounts?resetPasswords=start123")
curl.setopt(pycurl.SSL_VERIFYPEER, 0)
curl.setopt(pycurl.SSL_VERIFYHOST, 0)
curl.setopt(pycurl.CUSTOMREQUEST, "POST")
curl.perform()
print curl.getinfo(pycurl.RESPONSE_CODE)
curl.close()
```

## Request headers

```
POST /mapi/tenants/finance/userAccounts?resetPasswords=start123 HTTP/1.1
Host: admin.hcp.example.com:9090
Accept: */*
Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382
Content-Type: application/json
Content-Length: 2
```

## Response headers

HTTP/1.1 200 OKContent-Type: application/xmlX-HCP-SoftwareVersion: 9.0.0.2Content-Length: 0

## Data types

Many of the data types that describe HCP management API resources and properties are named, unordered sets of properties. When you create a resource, some properties for the applicable data type are required and some are optional. You need to specify a value for each required property. If you omit an optional property, HCP supplies a default value for it.

When you modify a resource, all properties for the applicable data type are optional. If you omit a property, the current value of the property remains unchanged.

When you create or modify a resource, HCP returns an error if the request body includes:

- Properties that are not valid for the resource
- Properties that are not valid for the request type
- Properties whose values cannot be set with the user account used for the request

Note: If a tenant has granted system-level users administrative access to itself, actions that can be performed with a tenant-level user account can also be performed with a system-level user account that includes the applicable roles.


Some resources also have required or optional query parameters.

### Common property values

These types of values, among others, are common to multiple HCP management API resources or properties.

## Passwords

User accounts have a password property. When creating or modifying a user account, the password is specified by the password query parameter.

When you create a tenant, you have the option of creating an initial user account for that tenant. In this case, you use query parameters, including the password parameter, to define that account.

To reset the passwords for all locally authenticated user accounts with the security role, you specify the new password in the resetPasswords query parameter.

When you enable WebDAV basic authentication, you need to specify a username and password. In this case, you use the `webdavBasicAuthPassword` property of the `httpProtocol` data type to specify the password.

Passwords can be up to 64 characters long, are case sensitive, and can contain any valid UTF-8 characters, including white space. To be valid, a password must include at least one character from two of these three groups: alphabetic, numeric, and other.

The minimum password length is configurable. To set the minimum password length, you use the `minimumPasswordLength` property of the `consoleSecurity` data type.

## Descriptions

Namespaces, namespace defaults, retention classes, user accounts, replication links, and erasure coding topologies all have a description property. Tenants have two properties for descriptions — `systemVisibleDescription` and `tenantVisibleDescription`.

All descriptions can be up to 1,024 characters long and can contain any valid UTF-8 characters, including white space.

All description properties are optional. If you omit this property when you create a namespace, the description defaults to the description specified by the namespace defaults for the tenant. For other resources, the default on creation is no description.

## Boolean values

For properties that take a Boolean value, valid values are:

- `true`, `t`, or `1` (one)
- `false`, `f`, or `0` (zero)

These values are case sensitive.

Invalid values are interpreted as false.

## Permission lists

These resources and properties have values that are lists of permissions:

- The permissions resource for tenants (specifies the permissions in the data access permission mask for a tenant)
- The permissions resource for namespaces (specifies the permissions in the data access permission mask for a namespace)
- The permissions property of the `namespacePermission` data type (used in the specification of the data access permissions for a user or group account)
- The `authMinimumPermissions` and `authAndAnonymousMinimum-Permissions` properties of the namespaces data type

The permissions that can be included in a list differ for the various resources and properties. However, in each case:

- In XML, the element that identifies each permission is `permission`. For example:


```
<permissions>
      <permission>READ</permission>
      <permission>WRITE</permission>
      <permission>DELETE</permission>
      <permission>PURGE</permission>
      <permission>SEARCH</permission>
</permissions>
```

- In JSON, the name in the name/value pair that lists the permissions is permission. For example:


```
"permissions" : {
      "permission" : [ "READ", "WRITE", "DELETE", "PURGE", "SEARCH" ]
}
```


### availableServicePlan

The availableServicePlan data type describes the availableServicePlans resource for tenants.

## Properties

The table below describes the properties included in the availableServicePlan data type.

| Property name | Data type | Description |
| --- | --- | --- |
| description | String | Specifies the description of the service plan. |
| name | String | Specifies the name of the service plan. |

## Example

Here’s an XML example of the availableServicePlan data type:

```
<availableServicePlan>
    <name>Platinum</name>
    <description>Most highly available, best performance -- suitable
        for data that will be frequently accessed or modified throughout its life</description>
</availableServicePlan>
```

### certificate

The `certificate` data type retrieves information about current and past replication certificates.

## certificate data type properties

The following table describes the property included in the `certificate` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| id | String | Specifies the system-supplied unique ID for the replication certificate. HCP generates this ID automatically when the certificate is generated. |  |
| subjectDN | String | Specifies the distinguished name of the certificate and its specified attributes |  |
| validOn | Date | Specifies the certificate activation date. |  |
| expiresOn | Date | Specifies the certificate expiration date. |  |

## Example

Here's an XML example of the `certificate` data type:

```
<certificate>
    <expiresOn>2021-12-12T15:35:10-0500</expiresOn>
    <id>server</id>
    <subjectDN>CN=*.example.com, OU=HCP, O=Hitachi, L=Waltham,
        ST=Massachusetts, C=US</subjectDN>
    <validOn>2016-12-12T15:35:10-0500</validOn>
</certificate>
```

### certificates

The certificates data type describes the `certificates` resource.

## certificates data type properties

The following table describes the property included in the `certificates` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| certificate | certificate | Specifies information about individual certificates. |  |

## Example

Here's an XML example of the `certificates` data type:

```
<certificates>
    <certificate>
        <expiresOn>2021-12-12T15:35:10-0500</expiresOn>
        <id>server</id>
        <subjectDN>CN=*.example.com, OU=HCP, O=Hitachi, L=Waltham, ST=Massachusetts, C=US</subjectDN>
        <validOn>2016-12-12T15:35:10-0500</validOn>
    </certificate>
</certificates>
```

### chargebackData

The `chargebackData` data type describes the `chargebackData` property of the `chargebackReport` data type. The `chargebackReport` data type is used to generate chargeback reports.

Most of the properties in the `chargebackData` data type represent statistics that describe the usage of a given namespace or of all the namespaces owned by a given tenant. In a chargeback report, each set of these statistics applies to one namespace or tenant during a given reporting interval (for example, one hour or one day).

Chargeback statistics either reflect a point in time or are dynamic. Point-in-time statistics are measurements taken at the end of a reporting interval, such as the used storage capacity for a namespace at the end of an hour. Dynamic statistics are measurements, such as the number of reads or writes to a namespace, that are accumulated over time.

HCP accumulates dynamic statistics on an hourly basis, starting at the beginning of each hour. So, for example, if the reporting interval is an hour, one statistic might represent the number of successful writes to a namespace that occurred between 11:00:00 and 11:59:59. If the reporting interval is a day, each reported dynamic statistic is the sum of the hourly values for the day, where the day starts at 00:00:00 and ends at 23:59:59.

## Properties

The table below describes the properties included in the `chargebackData` data type. When the output format for a `chargebackReport` resource is CSV, the properties included in the `chargebackData` data type are ordered. This table lists the properties in the order in which they appear in a CSV response body.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| systemName | String | One of:<br>- The name of the domain associated with the \[hcp\_system\] network for the HCP system to which the set of aggregated statistics in the line applies<br>- The name of the domain associated with the data access network for the tenant to which the set of aggregated statistics in the line applies<br>- The name of the domain associated with the data access network for the tenant that owns the namespace to which the set of statistics in the line applies |  |
| tenantName | String | Either:<br>- The name of the tenant to which the set of statistics applies<br>- The name of the tenant that owns the namespace to which the set of statistics applies |  |
| namespaceName | String | The name of the namespace to which the set of statistics applies. | For aggregated namespace statistics, this property has no value. |
| startTime | String | The start time of the reporting interval to which the set of statistics applies, in this ISO 8601 format:<br>`yyyy-MM-ddThh:mm:ssZ`<br>`Z` represents the offset from UTC, in this format:<br>( `+` \| `-`) `hhmm`<br>For example:<br>2017-02-18T14:00:00-0500 |  |
| endTime | String | The end time of the reporting interval to which the set of statistics applies, in the same ISO 8601 format as is used for the startTime property. |  |
| objectCount | Long | The total number of objects currently stored in the given namespace or in all the namespaces owned by the given tenant.<br>Each version of an object counts as a separate object. The object count does not include object versions that are delete markers or delete records.<br>Each multipart object counts as a single object. Objects that are in the process of being created by multipart uploads are not included in the object count. | This is a point-in-time statistic. |
| ingestedVolume | Long | The total size of the currently stored data and custom metadata, in bytes, before it was added to the given namespace or to all the namespaces owned by the given tenant. | This is a point-in-time statistic. |
| storageCapacity<br>Used | Long | The total number of bytes currently occupied by stored data in the given namespace or in all the namespaces owned by the given tenant. This includes object data, metadata, and any redundant data required to satisfy the applicable service plan.<br>Used storage capacity also includes storage occupied by the parts of in-progress multipart uploads that have already been written to HCP. Used storage capacity does not include replaced parts of multipart uploads, parts uploaded for aborted multipart uploads, or unused parts of completed multipart uploads. | This is a point-in-time statistic. |
| bytesIn | Long | The total number of bytes successfully written to the given namespace or to all the namespaces owned by the given tenant during the reporting interval.<br>The total number of bytes written includes all parts uploaded for multipart uploads regardless of whether the upload of the part has finished, the part was subsequently replaced, the multipart upload was aborted, or the part was not used in the completed multipart upload.<br>If data was compressed before being transmitted, this is the number of bytes before compression. | This is a dynamic statistic. |
| bytesOut | Long | The total number of bytes read from the given namespace or from all the namespaces owned by the given tenant during the reporting interval.<br>If data (including XML for directory listings) was compressed before being transmitted, this is the number of bytes before compression. | This is a dynamic statistic. |
| reads | Long | The total number of read operations performed in the given namespace or in all the namespaces owned by the given tenant during the reporting interval.<br>A read of a multipart object counts as a single read operation. | This is a dynamic statistic. |
| writes | Long | The total number of write operations successfully performed in the given namespace or in all the namespaces owned by the given tenant during the reporting interval.<br>Each upload of a part for a multipart upload counts as a separate write operation. This applies even if the part was subsequently replaced, the multipart upload was aborted, or the part was not used in the completed multipart upload. | This is a dynamic statistic. |
| deletes | Long | The total number of delete and purge operations performed in the given namespace or in all the namespaces owned by the given tenant during the reporting interval.<br>A delete of a multipart object counts as a single delete operation. The count of delete and purge operations does note include automatic deletions of replaced parts, parts of aborted multipart uploads, or unused parts of completed multipart uploads. | This is a dynamic statistic. |
| multipartObjects | Long | The total number of multipart objects currently stored in the given namespace or in all the namespaces owned by the given tenant. | This is a point-in-time statistic. |
| mulltipartObject Parts | Long | The total number of parts of multipart objects currently stored in the given namespace or in all the namespaces owned by the given tenant. | This is a point-in-time statistic. |
| mulltipartObject Bytes | Long | The total number of bytes of object data in all the parts of multipart objects currently stored in the given namespace or in all the namespaces owned by the given tenant. | This is a point-in-time statistic. |
| multipartUploads | Long | The total number of multipart uploads that are currently in progress in the given namespace or in all the namespaces owned by the given tenant. | This is a point-in-time statistic. |
| multipartUpload Parts | Long | The total number of successfully uploaded parts of multipart uploads that are currently in progress in the given namespace or in all the namespaces owned by the given tenant.<br>This number does not include replaced parts of multipart uploads, parts uploaded for aborted multipart uploads, or unused parts of completed multipart uploads. | This is a point-in-time statistic. |
| multipartUpload Bytes | Long | The total number of bytes of object data in all the successfully uploaded parts of multipart uploads that are currently in progress in the given namespace or in all the namespaces owned by the given tenant.<br>This number does not include bytes of data in replaced parts of multipart uploads, parts uploaded for aborted multipart uploads, or unused parts of completed multipart uploads. | This is a point-in-time statistic. |
| deleted | String | One of:<br>false<br> The namespace or tenant currently exists.included<br> For a tenant only, the statistics in the set include values for one or more namespaces that were subsequently deleted. |  |
| valid | Boolean | The status of the set of statistics in the line. Possible values are:<br>true<br> HCP successfully collected all statistics in the set.<br> false<br> The statistics in the set do not reflect all the activity that occurred during the reporting interval. This may be due, for example, to one or more nodes being unavailable during that time, to a network failure, or to other hardware issues. |  |

## Example

Here’s an XML example of the chargebackData data type that shows the properties for a namespace:

```
<chargebackData>
    <bytesIn>134243721</bytesIn>
    <bytesOut>87561</bytesOut>
    <deleted>false</deleted>
    <deletes>0</deletes>
    <endTime>2017-02-18T13:59:59-0500</endTime>
    <ingestedVolume>134243721</ingestedVolume>
    <multipartObjectBytes>93213889</multipartObjectBytes>
    <multipartObjectParts>7</multipartObjectParts>
    <multipartObjects>2</multipartObjects>
    <multipartUploadBytes>0</multipartUploadBytes>
    <multipartUploadParts>0</multipartUploadParts>
    <multipartUploads>0</multipartUploads>
    <namespaceName>finance</namespaceName>
    <objectCount>6</objectCount>
    <reads>1</reads>
    <startTime>2017-02-18T13:00:00-0500</startTime>
    <storageCapacityUsed>134270976</storageCapacityUsed>
    <systemName>hcp.example.com</systemName>
    <tenantName>europe</tenantName>
    <valid>true</valid>
    <writes>11</writes>
</chargebackData>
```

### chargebackReport

The `chargebackReport` data type describes the `chargebackReport` resource for tenants and namespaces. You use this resource to generate chargeback reports for tenants and namespaces.

A chargeback report contains historical statistics about a tenant or namespace, broken out either by hour or by day. You can also generate chargeback reports that contain a single set of statistics for a given time period, such as a specific month.

Chargeback reports can serve as input to billing systems that need to determine charges for capacity and bandwidth usage at the tenant or namespace level. Because a chargeback report can cover a specified time period, you can create applications that generate these reports at regular intervals and feed those reports to your billing system.

Tip: After a tenant or namespace is deleted, you can no longer generate chargeback reports for it. Therefore, to ensure that you don’t lose usage statistics, you should take this fact into consideration when setting the regular interval at which to generate these reports.


Chargeback reports are also a good source of information for system analysis, enabling you to adjust storage and bandwidth allocations based on usage patterns.

Chargeback reports are available only for HCP tenants and namespaces. You cannot generate a chargeback report for the default tenant or namespace.

A chargeback report for a namespace contains statistics only for that namespace. A chargeback report for a tenant contains aggregated namespace statistics. For example, the number of read operations for a tenant during a given reporting interval is the total of the numbers of read operations that occurred in all the namespaces owned by that tenant during that reporting interval.

You can use a system-level user account to request a tenant chargeback report regardless of whether the tenant has granted system-level users administrative access to itself. To generate a namespace chargeback report using a system-level user account, system-level users must have administrative access to the owning tenant.

When generating a chargeback report, you use query parameters on the resource URL in the GET request to specify the reporting interval and the time period you want the report to cover. HCP keeps chargeback statistics for 180 days. As a result, chargeback reports cannot report statistics from more that 180 days in the past.

Note: If you upgraded HCP less than 180 days ago from a release that does not support chargeback reports, the earliest available statistics are from the time the upgrade was completed.


The response to a chargebackReport GET request can be formatted as XML, JSON, or CSV.

## Query parameters

You use query parameters appended to the request URL to specify the time period and reporting interval for a chargeback report. These parameters are all optional. Default values are used for any you omit.

| Parameter type | Parameter name | Accepted values | Notes |
| --- | --- | --- | --- |
| Time period | start=start-time | Use the ISO 8601 format<br>If you specify both a start time and an end time, the start time must be earlier than the end time. | - With a reporting interval of hour or total, if you specify a start time that is not on an hour break, the first reporting interval in the report is the hour that includes the specified start time. For example, if you specify a start time of 9:45:00, the first reporting interval in the report starts at 9:00:00.<br>- With a reporting interval of day, if you specify a start time that is not on a day break, the first reporting interval in the report is the day that includes the specified start time. For example, if you specify a start time of 9:45:00 on October 6th, the first reporting interval in the report starts at 00:00:00 on October 6th.<br>- If you specify a start time that is earlier than the time of the earliest available chargeback statistics or if you omit the start parameter, the first reporting interval in the report is the interval that includes the earliest available statistics. |
| end=end-time | Use the ISO 8601 format<br>If you specify both a start time and an end time, the start time must be earlier than the end time. | - With a reporting interval of hour or total, the last reporting interval in the report is the hour that includes the specified end time. For example, if you specify an end time of 9:00:00, the last reporting interval in the report ends at 9:59:59.<br>- With a reporting interval of day, the last reporting interval in the report is the day that includes the specified end time. For example, if you specify an end time of 00:00:00 on October 6th, the last reporting interval in the report ends at 23:59:59 on October 6th.<br>- If you specify an end time that is later than the current time or if you omit the end parameter, the last reporting interval in the report is the interval that includes the current time. The point-in-time statistics reported for this interval are the statistics at the current time. The dynamic statistics are the statistics accumulated so far for the interval. |
| Reporting interval | granularity | hour<br> The reporting interval is one hour.day<br> The reporting interval is one day.total<br> The reporting interval is the time period defined by the start and end times for the report. In this case, the report contains a single set of chargeback statistics.<br>The default is `total`.<br>These values are not case sensitive. | The statistics reported for the current reporting interval, if included in the chargeback report, may not reflect some reads and writes that have already occurred during the current hour. After the hour is past, however, the statistics for that hour are complete. |

## Query parameter examples

To get hourly statistics for the entire day of February 18, 2017:

```
start=2017-02-18T00:00:00-0500&end=2017-02-18T23:59:59-0500
    &granularity=hour
```

To get daily statistics for the week starting February 19, 2017:

```
start=2017-02-19T00:00:00-0500&end=2017-02-25T23:59:59-0500
    &granularity=day
```

To get a single set of statistics for the entire month of February 2017:

```
start=2017-02-01T00:00:00-0500&end=2017-02-29T23:59:59-0500
    &granularity=total
```

To get hourly statistics for the current day from 8:00 a.m. up to the current time, where the current day is February 22, 2017:

```
start=2017-02-22T08:00:00-0500&granularity=hour
```

## Properties

The table below describes the property included in the `chargebackReport` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| chargebackData | chargeback<br>Data | Specifies statistics that describe the usage of a given namespace or of all the namespaces owned by a given tenant. | The response body includes one instance of this property for each reporting interval in the time period specified by the query parameters in the GET request for the chargebackReport resource. |

### cifsProtocol

The `cifsProtocol` data type describes the cifs resource for HCPnamespaces.

## Properties

The table below describes the properties included in the `cifsProtocol` data type.

| Property name | Data type | Description |
| --- | --- | --- |
| caseForcing | Boolean | Specifies whether the CIFS protocol is case forcing for the namespace. Valid values are:<br>UPPER<br> <br> The protocol changes object names to all uppercase in requests it passes to HCP.<br> LOWER<br> <br> The protocol changes object names to all lowercase in requests it passes to HCP.<br> DISABLED<br> The protocol is not case forcing.<br>The default is DISABLED.<br>These values are not case sensitive. |
| caseSensitive | Boolean | Specifies whether the CIFS protocol is case sensitive for the namespace. Valid values are:<br>true<br> The protocol is case sensitive.false<br> The protocol is not case sensitive.<br>The default is true. |
| enabled | Boolean | Specifies whether the CIFS protocol is enabled for the namespace. Valid values are:<br>true<br> CIFS is enabled.false<br> CIFS is disabled.<br>The default is false. |
| ipSettings | ipSettings | Specifies which IP addresses can and cannot access the namespace through the CIFS protocol. |
| requiresAuthentication | Boolean | Specifies whether user authentication is required or allowed for access to the namespace through the CIFS protocol. Valid values are:<br>true<br> User authentication is required.false<br> User authentication is allowed.<br>The default is true.<br>This property can be set to `true` only if the owning tenant supports AD authentication. |

## Example

Here’s an XML example of the cifsProtocol data type:

```
<cifsProtocol>
    <caseForcing>DISABLED</caseForcing/>
    <caseSensitive>true</caseSensitive>
    <enabled>true</enabled>
    <ipSettings>
         <allowAddresses>
             <ipAddress>192.168.140.10</ipAddress>
             <ipAddress>192.168.140.14</ipAddress>
             <ipAddress>192.168.140.15</ipAddress>
             <ipAddress>192.168.149.0/24</ipAddress>
         </allowAddresses>
         <denyAddresses>
             <ipAddress>192.168.149.5</ipAddress>
         </denyAddresses>
    </ipSettings>
    <requiresAuthentication>true</requiresAuthentication>
</cifsProtocol>
```

### complianceSettings

The `complianceSettings` data type describes the complianceSettings resource for namespaces.

## Properties

The table below describes the properties included in the `complianceSettings` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| customMetadataChanges | String | Specifies which operations are allowed with custom metadata for objects under retention. Valid values are:<br>ADD<br> Allows custom metadata to be added for objects under retention but not replaced or deletedALL<br> Allows custom metadata to be added, replaced, and deleted for objects under retentionNONE<br> Disallows all custom metadata operations for objects under retention<br>The default is ADD.<br>These values are not case sensitive. |  |
| dispositionEnabled | Boolean | Specifies whether objects with expired retention periods should automatically be deleted from the namespace. Valid values are:<br>true<br> Disposition is enabled.false<br> Disposition is not enabled.<br>The default is false. | For disposition to take effect at the namespace level, it must also be enabled in the HCP system configuration. |
| minimumRetentionAfterInitialUnspecified | String | For an HCP namespace, specifies that the retention set on `Initial Unspecified` objects must be greater than the `Minimum Retention after Initial Unspecified` value.<br>The default is 0 (zero) years, 0 months, and 0 days. | This property is not valid for the default namespace. |
| retentionDefault | String | For an HCP namespace, specifies the default retention setting for objects added to the namespace. Valid values are special values, offsets, retention classes, and fixed dates.<br>The default is 0 (zero), which is equivalent to `Deletion Allowed`. | This property is not valid for the default namespace. |
| shreddingDefault | Boolean | For an HCP namespace, specifies the default shred setting for objects added to the namespaces. Valid values are:<br>true<br> New objects are marked for shredding.false<br> New objects are not marked for shredding.<br>The default is false. | This property is not valid for the default namespace.<br>Once an object is marked for shredding, its shred setting cannot be changed. |

## Example

Here’s an XML example of the `complianceSettings` data type:

```
<complianceSettings>
    <customMetadataChanges>ALL</customMetadataChanges>
    <retentionDefault>A+7Y</retentionDefault>
    <minimumRetentionAfterInitialUnspecified>19y+0M+7d</minimumRetentionAfterInitialUnspecified>
    <dispositionEnabled>true</dispositionEnabled>
    <shreddingDefault>false</shreddingDefault>
</complianceSettings>
```

### connection

The `connection` data type describes the `connection` property of the `link` data type.

## connection data type properties

The following table describes the properties included in the `connection` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| localHost | String | Identifies the local system for the replication link. Valid values are:<br>- The domain name for the remote system for the link to use for communication with the local system (as that name is known to the remote system), in either of these formats:<br>  - replication.hcp-domain-<br>  - name<br>  - replication.admin.<br>  - hcp-domain-name<br>- One or more comma-separated IP addresses of storage nodes in the local system (as those addresses are known to the remote system)<br>For more information about these values, see the description of the remoteHost property.<br>Typically, you specify this property on a PUT request to create a link only if the system on which you’re creating the link uses network address translation (NAT) for communication with the other system. | This property is optional on a PUT request to create a replication link. |
| localPort | Integer | Specifies the port on which the local system for the replication link listens for data from the remote system. The default is 5748.<br>Typically, you specify a different port only if other port usage makes it necessary. | This property is optional on a PUT request to create a replication link. |
| remoteHost | String | Identifies the remote system for the replication link. Valid values are:<br>- The domain name of the remote system, in either of these formats:<br>  - replication.hcp-domain-<br>  - name<br>  - replication.admin.<br>  - hcp-domain-name<br>     <br>    hcp-domain-name must be the name of the domain associated with the network that’s selected for replication on the remote system. The second format is required if the domain for the replication network is shared with other networks.<br>- One or more comma-separated IP addresses of storage nodes in the remote system. These must be the node IP addresses in the network that’s selected for replication on the remote system.<br>   <br>  The local system for the link transmits data only to the nodes identified by the domain name or IP addresses you specify. Therefore, you should specify IP addresses only if you have a compelling reason to do so (for example, HCP is not using DNS, or you need to reduce the processing load on some number of nodes). | This property is required on a PUT request to create a replication link. |
| remotePort | Integer | Specifies the port on which the remote system for the replication link listens for data from the local system. The default is 5748.<br>Typically, you specify a different port only if other port usage makes it necessary. | This property is optional on a PUT request to create a replication link. |

## Example

Here’s an XML example of the `connection` data type:

```
<connection>
    <localHost>
    192.168.210.16, 192.168.210.17, 192.168.210.18, 192.168.210.19
    </localHost>
    <localPort>5748</localPort>
    <remoteHost>replication.admin.hcp-ca.example.com</remoteHost>
    <remotePort>5748</remotePort>
</connection>
```

### consoleSecurity

The `consoleSecurity` data type describes the `consoleSecurity` resource for tenants.

## Properties

The table below describes the properties included in the `consoleSecurity` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| automaticUserAccoutUnlockDuration | Integer | Specifies the amount of time an HCP user account can remain locked. Valid values are integers in the range zero through 999. A value of zero means accounts are never automatically unlocked. The default is 5 minutes. |  |
| automaticUserAccountUnlockSetting | boolean | Specifies whether the automatic unlock setting is active. True if the setting is active; otherwise, False. |  |
| blockCommonPassword | boolean | Specifies whether the setting to detect and block known or weak passwords is active. |  |
| blockPasswordReUse | boolean | Specifies whether the setting to detect and block password reuse is active. |  |
| coolDownPeriodDuration | Integer | Specifies the amount of time an HCP user account can remain locked due to failed login attempts. Valid values are integers in the range zero through 999. A value of zero means accounts are never automatically unlocked. The default is 5 seconds. |  |
| coolDownPeriodSettings | boolean | Specifies whether the cool down period setting is active. True if the setting is active; otherwise, False. |  |
| disableAfterAttempts | Integer | Specifies the number of times a locally authenticated or RADIUS-authenticated user can enter an incorrect password before the user account is automatically disabled. Valid values are integers in the range zero through 999. The default is five.<br>A value of zero means accounts are never disabled due to failed login attempts. | If the last locally authenticated user account with the security role is disabled due to failed login attempts and no group accounts have the security role, the user account is reenabled automatically after one hour. |
| disableAfterInactiveDays | Integer | Specifies the number of days an HCP user account can remain inactive before it’s automatically disabled. Valid values are integers in the range zero through 999. The default is 180 days.<br>A value of zero means accounts are never automatically disabled due to inactivity. | If no group accounts have the security role, the last locally authenticated user account with the security role is not automatically disabled due to inactivity. |
| forcePasswordChangeDays | Integer | Specifies the number of days after which locally authenticated users are automatically forced to change their passwords. Valid values are integers in the range zero through 999,999. The default is 180 days.<br>A value of zero means users are never automatically forced to change their passwords. |  |
| ipSettings | ipSettings | Specifies which IP addresses can and cannot access the Tenant Management Console. |  |
| loginMessage | String | Specifies message text to appear on the login page of the Tenant Management Console and Search Console. This text is optional. If specified, it can be up to 1,024 characters long and can contain any valid UTF-8 characters, including white space. |  |
| logoutOnInactive | Integer | Specifies the number of minutes a Tenant Management Console or Search Console session started with an explicit login can be inactive before it times out. Valid values are integers in the range zero through 999. The default is ten. |  |
| lowerCaseLetterCount | Integer | Specifies the number of lowercase letters (a through z). The default is 1. |  |
| minimumPasswordLength | Integer | Specifies the minimum number of characters for user account passwords. Valid values are integers in the range two through 64. The default is 6. |  |
| numericCharacterCount | Integer | Specifies the number of numeric characters required. The default is 1. |  |
| passwordCombination | boolean | Specifies whether the password complexity rules are enforced (for example, length, number of uppercase and lowercase letters). |  |
| passwordContainsUsername | boolean | Specifies whether the password can contain username. True if the setting is active; otherwise, False. |  |
| passwordReuseDepth | Integer | Specifies the number of previous passwords remembered. The default is 5. |  |
| specialCharacterCount | Integer | Specifies the number of Non-alphanumeric characters (for example: ~!@#$%^&\*\_-+=\`\|\\(){}\[\]:;"'<>,.?/). The default is 1. |  |
| upperCaseLetterCount | Integer | Specifies the number of uppercase letters (A through Z). The default is 1. |  |

## Example

Here’s an XML example of the consoleSecurity data type:

```
<consoleSecurity>
<automaticUserAccountUnlockSetting>false</automaticUserAccountUnlockSetting>
<automaticUserAccoutUnlockDuration>0</automaticUserAccoutUnlockDuration>
<blockCommonPassword>false</blockCommonPassword>
<blockPasswordReUse>false</blockPasswordReUse>
<coolDownPeriodDuration>5</coolDownPeriodDuration>
<coolDownPeriodSettings>false</coolDownPeriodSettings>
<disableAfterAttempts>5</disableAfterAttempts>
<disableAfterInactiveDays>180</disableAfterInactiveDays>
<forcePasswordChangeDays>180</forcePasswordChangeDays>
    <ipSettings>
	<allowAddresses>
		<ipAddress>192.168.103.18</ipAddress>
             <ipAddress>192.168.103.24</ipAddress>
             <ipAddress>192.168.103.25</ipAddress>
	</allowAddresses>
	<lowlfInBothLists>false</lowlfInBothLists>
	<denyAddresses/>
    </ipSettings>
<loginMessage> </loginMessage>
<logoutOnInactive>10</logoutOnInactive>
<lowerCaseLetterCount>0</lowerCaseLetterCount>
<minimumPasswordLength>6</minimumPasswordLength>
<numericCharacterCount>0</numericCharacterCount>
<passwordcombination>false</passwordcombination>
<passwordContainsUsername>true</passwordContainsUsername>
<passwordReuseDepth>4</passwordReuseDepth>
<specialCharacterCount>0</specialCharacterCount>
<upperCaseLetterCount>0</upperCaseLetterCount>
</consoleSecurity>
```

### contactInfo

The `contactInfo` data type describes the `contactInfo` resource for tenants.

## Properties

The table below describes the properties included in the `contactInfo` data type. All of these properties are optional. The property values can contain any valid UTF-8 characters, including white space.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| address1 | String | Specifies the first line of an address for the tenant contact |  |
| address2 | String | Specifies the second line of an address for the tenant contact |  |
| city | String | Specifies the city for the tenant contact |  |
| countryOrRegion | String | Specifies the country for the tenant contact |  |
| emailAddress | String | Specifies a valid email address for the tenant contact |  |
| extension | String | Specifies a telephone number extension for the tenant contact |  |
| firstName | String | Specifies the first name of the tenant contact |  |
| lastName | String | Specifies the last name of the tenant contact |  |
| primaryPhone | String | Specifies a telephone number for the tenant contact | Do not include a telephone number extension. Use the extension property for that. |
| state | String | Specifies the state or province for the tenant contact |  |
| zipOrPostalCode | String | Specifies the postal code for the tenant contact |  |

## Example

Here’s an XML example of the `contactInfo` data type:

```
<contactinfo>
    <address1>Exmple Co., Finance Dept.</address1>
    <address2>10 Main St.</address2>
    <city>Anytown</city>
    <countryOrRegion>USA</countryOrRegion>
    <emailAddress>lgreen@example.com</emailAddress>
    <extension>123</extension>
    <firstName>Lee</firstName>
    <lastName>Green</lastName>
    <zipOrPostalCode>12345</zipOrPostalCode>
    <primaryPhone>555-555-5555</primaryPhone>
    <state>MA</state>
</contactinfo>
```

### content

The content data type describes the `content`, `localCandidates`, and `remoteCandidates` resources for replication links.

## content data type properties

The following table describes the properties included in the `content` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| defaultNamespace<br>Directories | List | Lists the default-namespace directories that are candidates for or included in the replication link, as applicable. | This property is returned by a GET request only if the default tenant exists.<br>The listed directories are identified by directory name.<br>In XML, the element that identifies each directory is name. In JSON, the name in the name/value pair that lists the directories is name. |
| chainedLinks | List | For an active/passive link, lists the chained links that are candidates for or included in the replication link, as applicable. | This property is not returned by any GET request for the candidates for or content of an active/active link.<br>The listed chained links are identified by link name.<br>In XML, the element that identifies each chained link is name. In JSON, the name in the name/value pair that lists the chained links is name. |
| tenants | List | Lists the HCP tenants that are candidates for or included in the replication link, as applicable. | The listed tenants are identified by tenant name.<br>In XML, the element that identifies each tenant is name. In JSON, the name in the name/value pair that lists the tenants is name. |

## Example

Here’s an XML example of the `content` data type:

```
<content>
     <defaultNamespaceDirectories>
         <name>brochures_2017</name>
     </defaultNamespaceDirectories>
     <chainedLinks/>
     <tenants>
         <name>Finance</name>
         <name>HR</name>
         <name>Sales-Mktg</name>
     </tenants>
</content>
```

### contentClass

The `contentClass` data type describes the `contentClasses` resource for tenants.

## Properties

The table below describes the properties included in the `contentClass` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| contentProperties | content<br>Properties | Specifies the set of content properties in the content class. | This property is optional on a PUT request.<br>The set of content properties specified in the request body replaces the set of content properties currently in the content class. To remove all content properties, specify an empty set. |
| name | String | Specifies the name of the content class. Content class names must be from one through 64 characters long, can contain any valid UTF-8 characters, including white space, and are not case sensitive. | This property is required on a PUT request. |
| namespaces | List | Associates zero, one, or more namespaces with the content class. Valid values are the names of existing search-enabled namespaces. | This property is optional on a PUT request.<br>The set of namespaces specified in the request body replaces the set of namespaces currently associated with the content class. To dissociate all namespaces from the content class, specify an empty set.<br>In XML, element that identifies each namespace is `name`. In JSON, the name in the name/value pair that lists the namespaces is `name`. |

## Example

Here’s an XML example of the `contentClass` data type:

```
<contentClass>
    <name>DICOM</name>
    <contentProperties>
        <contentProperty>
            <name>Doctor_Name</name>
            <expression>/dicom_image/doctor/name</expression>
            <type>STRING</type>
            <multivalued>false</multivalued>
            <format></format>
        </contentProperty>
        <contentProperty>
            <name>Doctor_Specialty</name>
            <expression>/dicom_image/doctor/specialties/specialty</expression>
            <type>STRING</type>
            <multivalued>true</multivalued>
            <format></format>
        </contentProperty>
        <contentProperty>
            <name>Followup_Needed</name>
            <expression>/dicom_image/followup_needed</expression>
            <type>BOOLEAN</type>
            <multivalued>false</multivalued>
            <format></format>
        </contentProperty>
        <contentProperty>
            <name>Image_Date</name>
            <expression>/dicom_image/image/date</expression>
            <type>DATE</type>
            <multivalued>false</multivalued>
            <format>MM/dd/yyyy</format>
        </contentProperty>
        <contentProperty>
            <name>Image_Type</name>
            <expression>/dicom_image/image/@type</expression>
            <type>STRING</type>
            <multivalued>false</multivalued>
            <format></format>
        </contentProperty>
        <contentProperty>
            <name>Patient_ID</name>
            <expression>/dicom_image/patient/id</expression>
            <type>INTEGER</type>
            <multivalued>false</multivalued>
            <format></format>
        </contentProperty>
        <contentProperty>
            <name>Patient_Name</name>
            <expression>/dicom_image/patient/name</expression>
            <type>STRING</type>
            <multivalued>false</multivalued>
            <format></format>
        </contentProperty>
    </contentProperties>
    <namespaces>
        <name>Medical-Records</name>
    </namespaces>
</contentClass>
```

### contentProperty

The `contentProperties` data type describes the contentProperties property of the `contentClass` data type.

## Properties

The table below describes the properties included in the `contentProperty` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| expression | String | Specifies the expression for the content property. Valid values are valid XPath expressions, optionally prefixed with an annotation name, in this format:<br>@annot-name:xpath-expression | This property is required. |
| format | String | Specifies the format for the content property. Valid values are specific to each data type for which the format property is valid:<br>- For DATE, the value must be a valid datetime format. If you don't specify a format, the metadata query engine indexes only values that match patterns such as MM/DD/yyy, MM-dd-yyy, yyy-MM-dd, or yyy-MM-dd'T'HH:mm:ssZ.<br>- For FLOAT, the value must be a valid number format that can map content property vales to decimal numbers. If you don't specify a format, the metadata query engine indexes only sequences of digits that optionally include one decimal point.<br>- For INTEGER, the value must be a valid number format that can map content property values to integers. If you don't specify a format, the metadata query engine indexes only sequences of digits with no special characters. | This property is optional and can have a value only when the value of the types property is DATE, FLOAT, or INTEGER. |
| multivalued | Boolean | Specifies whether the content property is single-valued or multivalued. Valid values are:<br>true<br> The content property can have multiple values for any given object.false<br> The content property can have only one value for any given object.<br>The default is false. |  |
| name | String | Specifies the name of the content property. Content property names must be from one through 25 characters long, can contain only alphanumeric characters and underscores (\_), and are case sensitive. White space is not allowed. | This property is required. |
| type | String | Specifies the data type of the content property. Valid values are:<br>BOOLEAN<br> The metadata query engine indexes the value as true or false.DATE<br> The metadata query engine indexes the value as a date and time.FLOAT<br> The metadata query engine indexes the value as a decimal number with or without an exponent, depending on the value.FULLTEXT<br> The metadata query engine indexes the value as a text string after breaking it into tokens.INTEGER<br> The metadata query engine indexes the value as an integer.STRING<br> The metadata query engine indexes the content property values as text strings.<br>These values are not case sensitive. | This property is required. |

## Example

Here’s an XML example of the `contentProperty` data type:

```
<contentProperty>
    <name>Image_Date</name>
    <expression>/dicom_image/image/date</expression>
    <type>DATE</type>
    <multivalued>false</multivalued>
    <format>MM/dd/yyyy</format>
</contentProperty>
```

### contentProperties

The `contentProperties` data type describes the contentProperties property of the `contentClass` data type.

## Properties

The table below describes the property included in the `contentProperties` data type.

| Property name | Data type | Description |
| --- | --- | --- |
| contentProperty | content<br>Property | Specifies a content property. |

## Example

Here’s an XML example of the contentProperty data type:

```
<contentProperty>
    <name>Image_Date</name>
    <expression>/dicom_image/image/date</expression>
    <type>DATE</type>
    <multivalued>false</multivalued>
    <format>MM/dd/yyyy</format>
</contentProperty>
```

### cors data type

The `cors` data type describes the `cors` resource for tenants and namespaces.

The `cors` data type is a string in a `CDATA` section that specifies a CORS rules configuration.

## Example

Here is an example of the `cors` data type.

```
<cors><![CDATA[<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\
    <CORSConfiguration>\
          <CORSRule>\
               <AllowedOrigin>;:~`!@#$%^()-_+={}[]|\",.?/*</AllowedOrigin>\
               <AllowedMethod>PUT</AllowedMethod>\
               <AllowedHeader>&gt;someHeader</AllowedHeader>\
               <AllowedHeader>;~`!@#$%^()-_+={}[]|\".?/*</AllowedHeader>\
          </CORSRule>\
     </CORSConfiguration>\
]]></cors>
```

### customMetadataIndexingSettings

The `customMetadataIndexingSettings` data type describes the `customMetadataIndexingSettings` resource for namespaces.

## Properties

The table below describes the properties included in the `customMetadataIndexingSettings` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| contentClasses | List | Associates zero, one, or more content classes with the namespace. Valid values are the names of existing content classes.<br>Content class names are case sensitive. | The set of content classes specified in the request body replaces the set of content classes currently associated with the namespace. To dissociate all content classes from the namespace, specify an empty set.<br>In XML, element that identifies each content class is `name`. In JSON, the name in the name/value pair that lists the content classes is `name`. |
| fullIndexingEnabled | Boolean | Specifies whether the metadata query engine indexes the full text of custom metadata. Valid values are:<br>true<br> The metadata query engine indexes the full text of custom metadata.false<br> The metadata query engine does not index the full text of custom metadata.<br>The default is false. | You can set this property to `true` only while custom metadata indexing is enabled for the namespace. |
| excludedAnnotations | String | Specifies a comma-separated list of the names of annotations to be excluded from indexing by the metadata query engine.<br>Instead of explicit names, you can use patterns. The wildcard character for pattern matching is the asterisk (\*), which matches any number of characters of any type, including none. The asterisk can occur anywhere in the pattern.<br>Annotation names are case sensitive. | The list of annotation names you specify for this property replaces the current list of annotation names. To remove all annotation names from the list, specify this property with no value.<br>You can set a value for this property only while custom metadata indexing is enabled for the namespace.<br>Disabling custom metadata indexing for the namespace automatically deletes the list of excluded annotation for the namespace. |

## Example

Here’s an XML example of the `customMetadataIndexingSettings` data type:

```
<customMetadataIndexingSettings>
    <contentClasses>
         <name>DICOM</name>
         <name>Appointment</name>
    </contentClasses>
    <excludedAnnotations>misc*, email</excludedAnnotations>
    <fullIndexingEnabled>false</fullIndexingEnabled>
</customMetadataIndexingSettings>
```

### dataAccessPermissions

The `dataAccessPermissions` data type describes the `dataAccessPermissions` resource for group accounts and user accounts.

## Properties

The table below describes the property included in the `dataAccessPermissions` data type.

| Property | Data type | Description | Notes |
| --- | --- | --- | --- |
| namespacePermission | namespace<br>Permission | Specifies the permissions the user or group account has for a namespace. | Include one instance of this property for each namespace for which you want to change the permissions. If you omit a namespace, its permissions are not changed. |

## Example

Here’s an XML example of the `dataAccessPermissions` data type:

```
<dataAccessPermissions>
    <namespacePermission>
         <namespaceName>Accounts-Payable</namespaceName>
         <permissions>
             <permission>BROWSE</permission>
             <permission>READ</permission>
         </permissions>
    </namespacePermission>
    <namespacePermission>
         <namespaceName>Accounts-Receivable</namespaceName>
         <permissions>
             <permission>BROWSE</permission>
             <permission>SEARCH</permission>
             <permission>PURGE</permission>
             <permission>DELETE</permission>
             <permission>READ</permission>
             <permission>WRITE</permission>
         </permissions>
    </namespacePermission>
</dataAccessPermissions>
```

### ecTopology

The `ecTopology` data type describes the `ecTopologies` resource.

## ecTopology data type properties

The following table describes the properties included in the `ecTopology` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| description | String | Specifies the description of the erasure coding topology. This description is optional. The default is no description.<br>To remove the description from an existing erasure coding topology, specify the description property with no value. | This property is optional on a PUT request. |
| erasureCodedObjects | Long | Specifies the number of objects and parts of multipart objects on the local HCP system that were erasure coded according to this erasure coding topology. An object is counted as erasure coded if a chunk for it is stored on the system. | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request. |
| erasureCodingDelay | Integer | Specifies the erasure coding delay for the erasure coding topology as a number of days. Valid values are integers in the range zero through 3,650. The default is zero. | This property is optional on a PUT request. |
| fullCopy | Boolean | Specifies whether the erasure coding topology uses full-copy distribution or chunk distribution. Valid values are:<br>true<br> The erasure coding topology uses full-copy distribution.false<br> The erasure coding topology uses chunk distribution.<br>The default is false. | This property is optional on a PUT request. |
| hcpSystems | List | Lists the HCP systems that are included in the erasure coding topology. Each system is identified by the fully qualified name of the domain associated with the \[\[hcp\_system\]\] network on that system. | This property is not valid on a PUT or POST request.<br>In XML, the element that identifies each system is name. In JSON, the name in the name/value pair that lists the systems is name. |
| id | String | Specifies the ID for the erasure coding topology. | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request. |
| minimumObjectSize | Long | Specifies the minimum size for objects to be erasure coded. Valid values are:<br>- 4096<br>- 16384<br>- 32768<br>- 65536<br>- 131072<br>- 262144<br>- 524288<br>- 1048576<br>The default is 4096. | This property is optional on a PUT request. |
| name | String | Specifies the name of the erasure coding topology. The name must be from one through 64 characters long and can contain any valid UTF-8 characters, including white space. Erasure coding topology names are not case sensitive. | This property is required on a PUT request. |
| protectionStatus | String | Specifies the current status of the erasure coding topology with respect to how well-protected erasure-coded objects are. Possible values are:<br>BROKEN<br> Two or more systems in the topology are unavailable. Objects erasure coded according to the topology are inaccessible and are not protected.HEALTHY<br> All systems in the topology are available. Objects erasure coded according to the topology can be read and are fully protected.RETIRED<br> The topology has finished retiring.RETIRING<br> The topology is in the process of retiring.UNKNOWN<br> HCP cannot determine the protection status.<br> VULNERABLE<br> One system in the topology is unavailable, and the loss of a link or suspension of activity on a link would prevent an additional system from receiving data or chunks for newly ingested objects. Objects erasure coded according to the topology can be read but are not fully protected. | This property is not valid on a PUT or POST request. |
| readStatus | String | Specifies the current status of the erasure coding topology with respect to the ability to read erasure-coded objects. Possible values are:<br>BROKEN<br> Two or more systems in the topology are unavailable. Objects erasure coded according to the topology are inaccessible.HEALTHY<br> All systems in the topology are available. Objects erasure coded according to the topology can be read.RETIRED<br> The topology has finished retiring.UNKNOWN<br> HCP cannot determine the read status.<br> VULNERABLE<br> One system in the topology is unavailable. Objects erasure-coded according to the topology can be read, but the loss of a link would cause those objects to become inaccessible. | This property is not valid on a PUT or POST request. |
| replicationLinks | replication Links | Specifies the replication links included in the erasure coding topology. | This property is required on a PUT request. It is not valid on a POST request.<br>The properties returned for each replication link in response to a GET request for an erasure coding topology depend on whether the request includes the verbose=true query parameter. |
| restorePeriod | Integer | Specifies the restore period for the erasure coding topology as a number of days. Valid values are integers in the range zero through 180. The default is zero | This property is optional on a PUT request. |
| state | String | Specifies the state of the erasure coding topology. Possible values are:<br>ACTIVE<br> The topology is currently being used to erasure-code newly ingested objects that are subject to erasure coding.RETIRED<br> The topology is retired.RETIRING<br> The topology is in the process of retiring. | This property is not valid on a PUT or POST request. |
| tenants | List | Lists the tenants included in the erasure coding topology. | This property is not valid on a PUT or POST request.<br>In XML, the element that identifies each tenant is name. In JSON, the name in the name/value pair that lists the tenants is name. |
| type | String | Specifies the type of the underlying replication topology. Valid values for an erasure coding topology with four, five, or six systems are:<br>FULLY\_CONNECTED<br> The erasure coding topology is based on a fully connected active/active replication topology.RING<br> The erasure coding topology is based on an active/active replication ring topology.<br>For an erasure coding topology with three systems, the value of this property must be `FULLY_CONNECTED`.<br>These values are not case-sensitive. | This property is required on a PUT request. It is not valid on a POST request. |

## Example

Here's an XML example of the `ecTopology` data type; the properties shown are those that are returned in response to a verbose GET request:

```
<ecTopology>
    <description>Erasure coding topology for the US, Europe, Canada, and
        Africa-North divisions.</description>
    <erasureCodedObjects>3289</erasureCodedObjects>
    <erasureCodingDelay>10</erasureCodingDelay>
    <fullCopy>false</fullCopy>
    <hcpSystems>
        <name>hcp-an.example.com</name>
        <name>hcp-ca.example.com</name>
        <name>hcp-eu.example.com</name>
        <name>hcp-us.example.com</name>
    </hcpSystems>
    <id>faa9b2e5-a8b0-4211-ac83-6a25dff50800</id>
    <minimumObjectSize>4096</minimumObjectSize>
    <name>ex-corp-4</name>
    <protectionStatus>HEALTHY</protectionStatus>
    <readStatus>HEALTHY</readStatus>
    <replicationLinks>
        <replicationLink>
            <hcpSystems>
                <name>hcp-ca.example.com</name>
                <name>hcp-eu.example.com</name>
            </hcpSystems>
            <name>eu-ca</name>
            <pausedTenantsCount>0</pausedTenantsCount>
            <state>HEALTHY</state>
            <uuid>7ae4101c-6e29-426e-ae71-9a7a529f019d</uuid>
        </replicationLink>
        <replicationLink>
            <hcpSystems>
                <name>hcp-eu.example.com</name>
                <name>hcp-us.example.com</name>
            </hcpSystems>
            <name>us-eu</name>
            <pausedTenantsCount>0</pausedTenantsCount>
            <state>HEALTHY</state>
            <uuid>32871da5-2355-458a-90f5-1717aa684d6f</uuid>
        </replicationLink>
        <replicationLink>
            <hcpSystems>
                <name>hcp-an.example.com</name>
                <name>hcp-us.example.com</name>
            </hcpSystems>
            <name>us-an</name>
            <pausedTenantsCount>0</pausedTenantsCount>
            <state>HEALTHY</state>
            <uuid>c8c875ad-dbfe-437d-abd3-862a6c719894</uuid>
        </replicationLink>
        <replicationLink>
            <hcpSystems>
                <name>hcp-an.example.com</name>
                <name>hcp-ca.example.com</name>
            </hcpSystems>
            <name>ca-an</name>
            <pausedTenantsCount>0</pausedTenantsCount>
            <state>HEALTHY</state>
            <uuid>a1f21e03-fb46-48cc-967e-b0cedf80bb20</uuid>
        </replicationLink>
    </replicationLinks>
    <restorePeriod>5</restorePeriod>
    <state>ACTIVE</state>
    <tenants>
        <name>research-dev</name>
        <name>sales-mktg</name>
        <name>exec</name>
        <name>finance</name>
    </tenants>
    <type>RING</type>
</ecTopology>
```

## Query parameter for retiring an erasure coding topology

You use the retire query parameter to retire an erasure coding topology. You use this parameter on a POST request against the topology resource. You cannot include a request body with this request.

Here's a sample POST request that retires the erasure coding topology named ex-corp-3.

```
curl -k -d "<ecTopology/>"
    -H "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp-us.example.com:9090/mapi/services/erasureCoding/
        ecTopologies/ex-corp-3?retire"
```

## Query parameter for forcing the deletion of an erasure coding topology

You can delete an erasure coding topology only while either of these is true:

- At least one system in the topology is available, the total number of erasure-coded objects and erasure-coded parts of multipart objects on each available system is zero, and the state of the topology is retired. These are the normal conditions for deleting a topology.
- No more than one system in the topology is unavailable, the total number of erasure-coded objects and erasure-coded parts of multipart objects on each available system is zero, and the state of the topology is retiring. To delete the topology under these conditions, you need to include the force=true query parameter on the DELETE request.

Important: Deleting an erasure coding topology under these conditions may result in inaccessible data in namespaces on the unavailable system, even if those namespaces are configured to be compliant. You should delete the topology only if that system is no longer needed. If the system is still needed, wait to delete the topology until the topology has finished retiring on all systems.


Here's a sample DELETE request that deletes the erasure coding topology named ex-corp-3 while the topology meets the second set of conditions listed above:

```
curl -k -X DELETE
    -H "Authorization: HCP bGdyZWVu:35dc4c4aa08fe0deab7e292e00eb8e97"
    "https://admin.hcp-us.example.com:9090/mapi/services/erasureCoding/
        ecTopologies/ex-corp-3?force=true"
```

### emailNotification

The `emailNotification` data type describes the `emailNotification` resource for tenants.

## Properties

The table below describes the properties included in the `emailNotification` data type.

| Property | Data type | Description | Notes |
| --- | --- | --- | --- |
| emailTemplate | email<br>Template | Specifies the template for email messages HCP sends to the specified recipients. | The email template specified in the request body replaces the current email template. To restore the default email template, specify the emailTemplate property with no value.<br>For information about the default email template, see Email template defaults. |
| enabled | Boolean | Specifies whether email notification is enabled for the tenant. Valid values are:<br>true<br> Email notification is enabled.false<br> Email notification is disabled.<br>The default is false. | Email notification at the tenant level is supported only if HCP is configured to support it at the system level. |
| recipients | recipients | Specifies the set of recipients for email notification. | The set of recipients specified in the request body replaces the set of recipients currently configured for email notification. To remove all email recipients, specify an empty set.<br>Recipients are added to the Bcc list for each email. |

## Example

Here’s an XML example of the `emailNotification` data type:

```
<emailNotification>
    <enabled>true</enabled>
    <emailTemplate>
         <from>log@$location</from>
         <subject>$severity - $shortText</subject>
         <body>A message was written to the tenant log on $date.\n\n$reason\n\n
             $action</body>
    </emailTemplate>
    <recipients>
         <recipient>
              <address>lgreen@example.com, sgold@example.com</address>
              <importance>MAJOR</importance>
              <severity>ERROR</severity>
              <type>GENERAL,SECURITY</type>
          </recipient>
          <recipient>
              <address>mwhite@example.com</address>
              <importance>ALL</importance>
              <severity>WARNING</severity>
              <type>COMPLIANCE</type>
          </recipient>
    </recipients>
</emailNotification>
```

### emailTemplate

The `emailTemplate` data type describes the `emailTemplate` property of the `emailNotification` data type.

## Properties

The table below describes the properties included in the `emailTemplate` data type.

| Property | Data type | Description | Notes |
| --- | --- | --- | --- |
| body | String | Specifies the format of the body of the email messages HCP sends when email notification is enabled. Valid values include any combination of plain text and email template variables.<br>Plain text can include spaces and line breaks but not tabs. The character sequence consisting of a backslash (\\) followed by a lowercase n creates a line break. | To change the body in the email template to blank, include the from and subject properties in the request body and omit the body property. |
| from | String | Specifies the content of the email From line. Valid values include any combination of plain text and email template variables and must have the form of a valid email address.<br>Some email servers require that the value in the From line be an email address that is already known to the server. | If this property is included in the request body, the subject property must also be included. |
| subject | String | Specifies the content of the email Subject line. Valid values include any combination of plain text and email template variables.<br>Plain text can include spaces but not line breaks or tabs. | If this property is included in the request body, the from property must also be included. |

## Variables

The values you specify for the body, from, and subject properties of the `emailTemplate` data type can include variables that correspond to the information available for each log message (for example, the severity of the event that triggered the message or the short description of the event). When sending email, HCP replaces the variables in the email message with the applicable information.

To include a variable in the email template, you specify the variable name preceded by the dollar sign ($). A dollar sign followed by anything other than a variable name is displayed as a dollar sign in the email HCP sends.

The list below lists the variables you can use in the email template.

$action
The action to take in response to the message$date
The date and time at which the event occurred (for example, Wed Feb 8 2017 3:15:57 PM EST)$fullText
The full text of the message$id
The message ID$location

The fully qualified name of the HCP system on which the event occurred (for example, hcp-ma.example.com)
$origin

For user-initiated events, the IP address from which the event request was sent and the port through which HCP received the event request, separated by a colon (for example, 192.168.152.181:8000)
$reason

The reason why HCP issued the message
$severity
The severity of the event that triggered the message$shortText
A brief description of the event that triggered the message$type
The type of message (General, Security, or Compliance), preceded by Important and a comma if the message is important (for example, Important, Security)$user
The user ID and username of the event initiator (for example, 105ff38f-4770-4f98-b5b3-8371ab0af359 lgreen)

## Defaults

The list below shows the format of the default email template.

From
log@$locationSubject
\[$severity\] $shortTextBody


```
The following event occurred on $date:
$fullText

Reason:
$reason

Action:
$action

Details:
User: $user
Origin: $origin
```

## Example

Here’s an XML example of the `emailTemplate` data type:

```
<emailTemplate>
    <from>log@$location</from>
    <body>A message was written to the tenant log on $date.\n\n$reason\n\n
         $action</body>
    <subject>$severity - $shortText</subject>
</emailTemplate>
```

### failoverSettings

The `failoverSettings` data type describes the `failoverSettings` property of the `link` data type.

## failoverSettings data type properties

The following table describes the properties included in the `failoverSettings` data type.

| Property name | Data type | Description | Notes |
| --- | --- | --- | --- |
| local | local | Specifies the automatic failover and failback settings for the local system for the replication link. | This property is optional on a PUT request to create a replication link. |
| remote | remote | Specifies the automatic failover and failback settings for the remote system for the replication link. | This property is optional on a PUT request to create a replication link. |

## Example

Here’s an XML example of the `failoverSettings` data type; the properties shown are those that are returned by a GET request sent to the primary system for an active/passive link:

```
<failoverSettings>
    <remote>
         <autoFailover>true</autoFailover>
         <autoFailoverMinutes>120</autoFailoverMinutes>
         <autoCompleteRecovery>false</autoCompleteRecovery>
         <autoCompleteRecoveryMinutes>60</autoCompleteRecoveryMinutes>
    </remote>
</failoverSettings>
```

### groupAccount

The `groupAccount` data type describes the `groupAccounts` resource.

## Properties

The table below describes the properties included in the `groupAccount` data type.

| Property | Data Type | Description | Notes |
| --- | --- | --- | --- |
| allowNamespace<br>Management | Boolean | Specifies whether the group account has the allow namespace management property. Valid values are:<br>true<br> The group account has the allow namespace property.false<br> The group account does not have the allow namespace management property.<br>On a PUT request, the default is `true` if the roles property includes ADMINISTRATOR in the same request; otherwise, the default is `false`.<br>On a POST request, adding ADMINISTRATOR to the roles for the group account automatically enables the allow namespace management property for the account.<br>Users in groups with the allow namespace management property can use the HCP management and S3 compatible APIs to:<br>- Create namespaces<br>- List, view and change the versioning status of, and delete namespaces they own | This property is not valid on a PUT request. It is valid on a POST request only if the user making the request has the administrator role. |
| externalGroupID | String | Specifies the security identifier (SID) of the AD group that corresponds to the HCP group account. For a PUT request, valid values are the SIDs of AD groups defined in the AD forest supported by HCP. | Either this property or the groupname property is required on a PUT request. If you include both properties in the request body, they must identify the same AD group.<br>This property is not valid on a POST request. It is returned only by a verbose GET request and only when the user making the request has the security role. |
| groupname | String | Specifies the name of the HCP group account. For a PUT request, valid values are the names of AD groups defined in the AD forest supported by HCP, in either of these formats:<br>group-name<br>group-name@ad-domain- name<br>If you omit the domain name, HCP uses the AD domain specified in the system configuration.<br>Be sure to use the second format if a group with the specified name exists in more than one domain in the AD forest or if the group name looks like a SID. | Either this property or the `externalGroupID` property is required on a PUT request. If you include both properties in the request body, they must identify the same AD group.<br>This property is not valid on a POST request. |
| roles | List | Associates zero, one, or more roles with the group account. Valid values for roles are:<br>- ADMINISTRATOR<br>- COMPLIANCE<br>- MONITOR<br>- SECURITY<br>These values are not case sensitive.<br>The default is no roles. | This property is valid on a POST request and returned by a GET request only when the user making the request has the security role.<br>A user with the ADMINISTRATOR role cannot POST this property.<br>For an existing group account, the set of roles specified in the request body replaces the set of roles currently associated with the group account. To remove all roles, specify an empty set.<br>In XML, the element that identifies each role is `role`. In JSON, the name in the name/value pair that lists the roles is `role`. |

## Example

Here’s an XML example of the `groupAccount` data type:

```
<groupAccount>
    <allowNamespaceManagement>false</allowNamespaceManagement>
    <externalGroupID>S-1-5-21-1522923621-2272695913-102089983-3621
    </externalGroupID>
    <groupname>hcp-admin@ad.example.com</groupname>
    <roles>
         <role>MONITOR</role>
         <role>ADMINISTRATOR</role>
    </roles>
</groupAccount>
```

### healthCheckDownload

The `healthCheckDownload` data type describes the health check reports download for specified HCP cluster nodes.

## healthCheckDownload data type properties

The following table describes the properties included in the `healthCheckDownload` data type.

| Property | Data type | Description |
| --- | --- | --- |
| nodes | String | Specifies the HCP cluster nodes for which you want to download health check reports. The nodes are specified in a comma-separated list. |
| content | String | Specifies the content that you want to download for the specified HCP cluster nodes.<br> <br>Note: As of HCP 9.2, the content property is obsolete. This property remains in the documentation for backward compatibility only. |

## Example

Here is an XML example of the `healthCheckDownload` data type:

```
<healthCheckDownload>
    <nodes>107,120</nodes>
</healthCheckDownload>
```

### healthCheckDownloadStatus

The `healthCheckDownloadStatus` data type describes the download status of the health check reports archive for specified HCP cluster nodes.

## healthCheckDownloadStatus data type properties

The following table describes the properties included in the `healthCheckDownloadStatus` data type.

| Property | Data type | Description |
| --- | --- | --- |
| readyForStreaming | Boolean | Specifies whether the health check reports are ready for download. |
| streamingInProgress | Boolean | Specifies whether the health check reports download phase is in progress. |
| error | Boolean | Specifies whether an error has occurred during the reports preparation or download phase. |
| started | Boolean | Specifies whether the health check reports preparation phase has started. |
| content | String | Specifies the type of download report. |

## Example

Here is an XML example of the `healthCheckDownloadStatus` data type:

```
<healthCheckDownloadStatus>
     <readyForStreaming>true</readyForStreaming>
     <streamingInProgress>false</streamingInProgress>
     <error>false</error>
     <started>true</started>
     <content>HCR</content>
</healthCheckDownloadStatus>
```

### healthCheckPrepare

The `healthCheckPrepare` data type describes the information needed to prepare the health check reports for the available HCP cluster nodes.

## healthCheckPrepare data type properties

The following table describes the properties included in the `healthCheckPrepare` data type.

| Property | Data type | Description |
| --- | --- | --- |
| exactTime | String | (Optional) If this property is specified, the reports for each node will have a start date and time of `startDate`  `exactTime`.<br> <br>Specifying this property ensures that if you download health check reports daily at the same time, the downloads will not include the same `.tar` archives on two consecutive days. |
| collectCurrent | Boolean | (Optional) If this property is set to `false`, when data collection for the specified start and end dates is performed, data will not be collected for the current report.<br> <br>The default value is `true`. |
| startDate | String | (Optional) Data collection start date for preparing the health check reports.<br> <br>The default value is today. |
| endDate | String | (Optional) Data collection end date for preparing the health check reports.<br> <br>The default value is today if the `exactTime` property value is not specified; otherwise, the `endDate` is yesterday. |

## Example

Here is an XML example of the `healthCheckPrepare` data type:

```
<healthCheckPrepare>
     <startDate>09/19/2020</startDate>
     <endDate>09/21/2020</endDate>
     <exactTime>04:00</exactTime>
     <collectCurrent>false</collectCurrent>
</healthCheckPrepare>
```

### httpProtocol

The `httpProtocol` data type describes the http resource for HCPnamespaces. This data type includes properties for the REST, S3 compatible, and WebDAV namespace access protocols.

## Properties

The table below describes the properties included in the `httpProtocol` data type.

| Property | Data type | Description | Notes |
| --- | --- | --- | --- |
| hs3Enabled | Boolean | Specifies whether the Hitachi API for Amazon S3 is enabled for the namespace. Valid values are:<br> true<br> <br> The Hitachi API for Amazon S3 is enabled.<br> false<br> <br> The Hitachi API for Amazon S3 is disabled.<br> <br>The default is false. | This property can be set to `true` only if ACLs are enabled for the namespace. |
| hs3Requires Authentication | Boolean | Specifies whether user authentication is required or optional for access to the namespace through the Hitachi API for Amazon S3. Valid values are:<br> true<br> User authentication is required.false<br> User authentication is optional.<br>The default is true. |  |
| httpActiveDirectorySSO<br>Enabled | Boolean | Specifies whether HCP supports AD single sign-on for access to the namespace through the REST and S3 compatible APIs. Valid values are:<br> true<br> HCP supports AD single sign-on for access to the namespace through the REST and S3 compatible APIs.<br> false<br> HCP does not support AD single sign-on for access to the namespace through the REST and S3 compatible APIs.<br> <br>The default is true. | This property is valid on a POST request only if the owning tenant supports AD authentication.<br>You can set the value of this property to `true` only while HTTP or HTTPS is enabled. Disabling both HTTP and HTTPS automatically disables this property. |
| httpEnabled | Boolean | Specifies whether the HTTP port is open for REST API and WebDAV access to the namespace without SSL security. Valid values are:<br> true<br> The HTTP port is open.false<br> The HTTP port is closed.<br>The default is false. |  |
| httpsEnabled | Boolean | Specifies whether the HTTPS port is open for REST API and WebDAV access to the namespace with SSL security. Valid values are:<br> true<br> The HTTPS port is open.false<br> The HTTPS port is closed.<br>The default is true. | Certain countries restrict the use of HTTPS. If the HCP system does not have HTTPS enabled, the httpsEnabled value cannot be set to `true`. |
| ipSettings | ipSettings | Specifies which IP addresses can and cannot access the namespace through the HTTP and WebDAV protocols. |  |
| restEnabled | Boolean | Specifies whether the REST API is enabled for the namespace. Valid values are:<br> true<br> The REST API is enabled.false<br> The REST API is disabled.<br>The default is true. |  |
| restRequires Authentication | Boolean | Specifies whether user authentication is required or optional for access to the namespace through the REST API. Valid values are:<br> true<br> User authentication is required.false<br> User authentication is optional.<br>The default is true. |  |
| webdavBasicAuth Enabled | Boolean | Specifies whether the WebDAV protocol requires basic authentication for access to the namespace. Valid values are:<br> true<br> WebDAV requires basic authentication.false<br> WebDAV does not require basic authentication.<br>The default is false. | You can set the value of this property to `true` only if a WebDAV username and password already exist or are specified by the webdav-BasicAuthUsername and webdavBasicAuthPassword properties in the same request. |
| webdavBasicAuth Password | String | Specifies the password to use for basic authentication with the WebDAV protocol. | You can specify a value for this property only if a basic authentication username already exists or is specified by the `webdavBasicAuth-Username` property in the same request.<br>To remove the basic authentication password, specify the `webdavBasic-AuthPassword` property with no value. You can remove the password only if you remove the basic authentication username in the same request. |
| webdavBasicAuth Username | String | Specifies the username to use for basic authentication with the WebDAV protocol.<br>Usernames must be from one through 64 characters long and can contain any valid UTF-8 characters but cannot start with an opening square bracket (\[). White space is allowed.<br>Usernames are not case sensitive. | You can specify a value for this property only if a basic authentication password already exists or is specified by the `webdavBasicAuth-Password` property in the same request.<br>To remove the basic authentication username, specify the webdavBasic- `AuthUsername` property with no value. You can remove the username only if you remove the basic authentication password in the same request. |\
| webdavCustom Metadata | Boolean | Specifies whether WebDAV dead properties can be stored as custom metadata. Valid values are:<br> true<br> Dead properties can be stored as custom metadata.false<br> Dead properties cannot be stored as custom metadata.<br>The default is false. |  |\
| webdavEnabled | Boolean | Specifies whether the WebDAV protocol is enabled for the namespace. Valid values are:<br> true<br> The WebDAV protocol is enabled.false<br> The WebDAV protocol is disabled.<br>The default is false. |  |\
\
## Example\
\
Here’s an XML example of the `httpProtocol` data type:\
\
```\
<httpProtocol>\
    <hs3Enabled>false</hs3Enabled>\
    <hs3RequiresAuthentication>false</hs3RequiresAuthentication>\
    <httpActiveDirectorySSOEnabled>true</httpActiveDirectorySSOEnabled>\
    <httpEnabled>false</httpEnabled>\
    <httpsEnabled>true</httpsEnabled>\
    <ipSettings>\
         <allowAddresses>\
             <ipAddress>192.168.140.10</ipAddress>\
             <ipAddress>192.168.140.14</ipAddress>\
             <ipAddress>192.168.140.15</ipAddress>\
             <ipAddress>192.168.149.0/24</ipAddress>\
         </allowAddresses>\
         <allowIfInBothLists>false</allowIfInBothLists>\
         <denyAddresses>\
             <ipAddress>192.168.149.5</ipAddress>\
         </denyAddresses>\
    </ipSettings>\
    <restEnabled>true</restEnabled>\
    <restRequiresAuthentication>true</restRequiresAuthentication>\
    <webdavBasicAuthEnabled>false</webdavBasicAuthEnabled>\
    <webdavBasicAuthPassword></webdavBasicAuthPassword>\
    <webdavBasicAuthUsername></webdavBasicAuthUsername>\
    <webdavCustomMetadata>false</webdavCustomMetadata>\
    <webdavEnabled>false</webdavEnabled>\
</httpProtocol>\
```\
\
### ipSettings\
\
The `ipSettings` data type describes the `ipSettings` property of these data types:\
\
- cifsProtocol\
- consoleSecurity\
- httpProtocol\
- nfsProtocol\
- protocols\
- searchSecurity\
- smtpProtocol\
\
## Properties\
\
The table below describes the properties included in the `ipSettings` data type.\
\
| Property | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| allowAddresses | allow<br>Addresses | Specifies the IP addresses to be allowed access to HCP through the given interface. By default, the set of IP addresses includes only 0.0.0.0/0. | The set of IP addresses specified in the request body replaces the set of IP addresses currently allowed access to HCP through the given interface. To remove all IP addresses, specify an empty set.<br>In XML, each IP address specification is the value of an element named `ipAddress`. In JSON, the name in the name/value pair that specifies the IP addresses is `ipAddress`. |\
| allowIfInBothLists | Boolean | Specifies how HCP should handle IP addresses that are either both allowed and denied or neither allowed nor denied access to HCP through the given interface. Valid values are `true` and `false`. The default is true.<br>For the effects of specifying `true` or `false`, see `allowIfInBothLists` property effects list below. | This property is not valid for the `cifsProtocol`, `nfsProtocol`, and `smtpProtocol` data types. |\
| denyAddresses | deny<br>Addresses | Specifies the IP addresses to be denied access to HCP through the given interface. By default, the set of IP addresses is empty. | This property is not valid for the `nfsProtocol` data type.<br>The set of IP addresses specified in the request body replaces the set of IP addresses currently denied access to HCP through the given interface. To remove all IP addresses, specify an empty set.<br>In XML, each IP address specification is the value of an element named `ipAddress`. In JSON, the name in the name/value pair that specifies the IP addresses is `ipAddress`. |\
\
## allowAddresses and denyAddresses list entries\
\
Each ipAddress entry within an `allowAddresses` or `denyAddresses` property can have a value of:\
\
- An individual IP address.\
- A comma-separated list of IP addresses.\
- A range of IP addresses specified as ip-address/subnet-mask (for example, 192.168.100.197/255.255.255.0).\
- A range of IP addresses specified in CIDR format (for example, 192.168.100.0/24). The CIDR entry that matches all IP addresses is 0.0.0.0/0.\
\
## allowIfInBothLists property effects\
\
The table below describes the effects of specifying true or false for the `allowIfInBothLists` property.\
\
| Listed IP addresses | true | false |\
| --- | --- | --- |\
| allowAddresses: none<br>denyAddresses: none | All IP addresses can access HCP through the given interface. | No IP addresses can access HCP through the given interface. |\
| allowAddresses: at least one<br>denyAddresses: none | All IP addresses can access HCP through the given interface. | Only IP addresses in the allowAddresses list can access HCP through the given interface. |\
| allowAddresses: none<br>denyAddresses: at least one | All IP addresses not in the denyAddresses list can access HCP through the given interface. IP addresses in the denyAddresses list cannot. | No IP addresses can access HCP through the given interface. |\
| allowAddresses: at least one<br>denyAddresses: at least one | IP addresses appearing in both or neither of the lists can access HCP through the given interface. | IP addresses appearing in both or neither of the lists cannot access HCP through the given interface. |\
\
## Example\
\
Here’s an XML example of the `ipSettings` data type:\
\
```\
<ipSettings>\
    <allowAddresses>\
         <ipAddress>192.168.140.10</ipAddress>\
         <ipAddress>192.168.140.14</ipAddress>\
         <ipAddress>192.168.140.15</ipAddress>\
         <ipAddress>192.168.149.0/24</ipAddress>\
    </allowAddresses>\
    <allowIfInBothLists>false</allowIfInBothLists>\
    <denyAddresses>\
         <ipAddress>192.168.149.5</ipAddress>\
    </denyAddresses>\
</ipSettings>\
```\
\
### license\
\
The `license` data type retrieves information about current and past storage licenses.\
\
## license data type properties\
\
The following table describes the properties included in the `license` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| localCapacity | Long | Specifies the active storage capacity in terabytes. | Value is returned in bytes. |\
| expirationDate | String | Specifies the storage license expiration date.<br>If there is no expiration date, enter None. |  |\
| extendedCapacity | Long | Specifies the extended storage capacity in terabytes. | Value is returned in bytes. |\
| feature | String | Specifies the type of license. Valid values are:<br>- Basic<br>- Premium |  |\
| serialNumber | String | Specifies the serial number of the HCP system the storage license is intended for. |  |\
| uploadDate | String | Specifies the date that the license was uploaded. | This property is returned only by a verbose GET request. |\
\
## Example\
\
Here’s an XML example of the `license` data type:\
\
```\
<license>\
         <localCapacity>10000000000000</activeCapacity>\
         <expirationDate>Jan 1, 2021</expirationDate>\
         <extendedCapacity>0</extendedCapacity>\
         <feature>Premium</feature>\
         <serialNumber>12345</serialNumber>\
         <uploadDate>Aug 14, 2016</uploadDate>\
</license>\
```\
\
### licenses\
\
The `licenses` data type describes the `storage licenses` resource.\
\
## licenses data type properties\
\
The following table describes the property included in the `licenses` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| License | License | Specifies information about individual storage license. | The response to a GET for the licenses resource returns either a single instance of the license property or multiple instances depending on whether the request includes the verbose query parameter. |\
\
## Example\
\
Here’s an XML example of the `licenses` data type:\
\
```\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\
<licenses>\
     <license>\
         <localCapacity>10000000000000</activeCapacity>\
         <expirationDate>Jan 1, 2021</expirationDate>\
         <extendedCapacity>0</extendedCapacity>\
         <feature>Premium</feature>\
         <serialNumber>12345</serialNumber>\
         <uploadDate>Aug 14, 2016</uploadDate>\
    </license>\
</licenses>\
```\
\
## Query parameter for retrieving a list of licenses\
\
To retrieve a list of all the storage licenses that have ever been uploaded to the HCP system, you use this query parameter:\
\
```\
verbose=true\
```\
\
Here’s a sample GET request that retrieves both the current and past storage licenses for an HCP system:\
\
```\
curl -k -b -H “Accept:application/xml\
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"\
    https://admin.hcp.example.com:9090/mapi/storage/licenses?verbose=true\
```\
\
### link\
\
The `link` data type describes the `links` resource.\
\
## link data type properties\
\
The following table describes the properties included in the `link` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| compression | Boolean | Specifies whether HCP should compress data before transmitting it to the other system involved in the replication link. For an active/active link, this setting applies to data being replicated in both directions on the link.<br>Valid values are:<br>true<br> HCP should compress data.<br> false<br> HCP should not compress data.<br> <br>The default is false. | This property is optional on a PUT request. |\
| connection | connection | Identifies the remote system for the replication link and specifies how the two systems should communicate with each other. | This property is required on a PUT request. |\
| description | String | Specifies a description of the replication link. This description is optional. The default is no description.<br>To remove a description from an existing link, specify the description property with no value. | This property is optional on a PUT request. |\
| encryption | Boolean | Specifies whether HCP should encrypt data before transmitting it to the other system involved in the replication link. For an active/active link, this setting applies to data being replicated in both directions on the link.<br>Valid values are:<br>true<br> HCP should encrypt data.<br> false<br> HCP should not encrypt data.<br> <br>The default is false. | This property is optional on a PUT request. |\
| failoverSettings | failover<br>Settings | Specifies the automatic failover and failback settings for the replication link. | This property is optional on a PUT request. |\
| id | String | Specifies the system-supplied unique ID for the replication link. HCP generates this ID automatically when you create a link. | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request. |\
| name | String | Specifies the name of the replication link. Link names must be from one through 64 characters long and can contain any valid UTF-8 characters, including white space. Link names are not case sensitive.<br>Link names must be unique within a replication topology. | This property is required on a PUT request. |\
| priority | String | Specifies whether priority should be given to objects with the oldest changes, regardless of namespace, or processing should be balanced across namespaces. For an active/active link, this setting applies to both systems involved in the link.<br>Valid values are:<br>FAIR<br> Balance processing across namespaces.OLDEST\_FIRST<br> Give priority to objects with the oldest changes.<br>The default is OLDEST\_FIRST.<br>These values are not case-sensitive. | This property is optional on a PUT request. |\
| statistics | statistics | Specifies information about activity on the replication link. | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request. |\
| status | String | Specifies the general state of the replication link. Possible values are:<br>GOOD<br> The link is healthy.WARNING<br> The link is healthy, but normal replication is not occurring on the link.BAD<br> The link is unhealthy. | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request. |\
| statusMessage | String | Specifies text describing the current state of the replication link. Possible values are:<br>- Synchronizing data<br>- Sending data<br>- Receiving data<br>- Recovering data<br>- Completing recovery<br>- Scheduled off period<br>- Suspended by user<br>- Pending remote reply<br>- Pending<br>- Failed over<br>- Remote storage full, link suspended<br>- Local storage full, link suspended<br>- High error rate<br>- Stalled link<br>- Broken link | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request. |\
| suspended | Boolean | Specifies whether the replication link is currently suspended. Possible values are:<br>true<br> The link is suspendedfalse<br> The link is not suspended. | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request. |\
| type | String | Specifies the replication link type. Valid values are:<br>ACTIVE\_ACTIVE<br> The link is an active/active link.OUTBOUND<br> The link is an outbound active/passive link.INBOUND<br> The link is an inbound active/passive link.<br>These values are not case-sensitive. | This property is required on a PUT request.<br>You can change the value of this property from OUTBOUND or INBOUND to ACTIVE\_ACTIVE. You cannot change the value from:<br>•ACTIVE\_ACTIVE to OUTBOUND or INBOUND<br>•OUTBOUND to INBOUND<br>•INBOUND to OUTBOUND |\
\
## Example\
\
Here’s an XML example of the `link` data type; the properties shown are those that are returned by a verbose GET request for an active/active link:\
\
```\
<link>\
    <compression>false</compression>\
    <connection>\
        <localHost>\
        192.168.210.16, 192.168.210.17, 192.168.210.18, 192.168.210.19\
        </localHost>\
        <localPort>5748</localPort>\
        <remoteHost>replication.admin.hcp-ca.example.com</remoteHost>\
        <remotePort>5748</remotePort>\
    </connection>\
    <description>Active/active link between systems in MA and CA</description>\
    <encryption>false</encryption>\
    <failoverSettings>\
        <local>\
            <autoFailover>true</autoFailover>\
            <autoFailoverMinutes>60</autoFailoverMinutes>\
        </local>\
        <remote>\
            <autoFailover>true</autoFailover>\
            <autoFailoverMinutes>60</autoFailoverMinutes>\
        </remote>\
    </failoverSettings>\
    <id>2de89eec-0ec0-4f98-b852-0778dfeee792</id>\
    <name>MA-CA</name>\
    <priority>OLDEST_FIRST</priority>\
    <statistics>\
        <upToDateAsOfString>2017-02-18T10:47:59-0400\
        </upToDateAsOfString>\
        <upToDateAsOfMillis>1395154079640</upToDateAsOfMillis>\
        <bytesPending>189027593061</bytesPending>\
        <bytesPendingRemote>319740</bytesPendingRemote>\
        <bytesReplicated>72254119306967</bytesReplicated>\
        <bytesPerSecond>56215390</bytesPerSecond>\
        <objectsPending>534</objectsPending>\
        <objectsPendingRemote>2</objectsPendingRemote>\
        <objectsReplicated>295661</objectsReplicated>\
        <operationsPerSecond>119.1</operationsPerSecond>\
        <errors>0</errors>\
        <errorsPerSecond>0.0</errorsPerSecond>\
        <objectsVerified>402200</objectsVerified>\
        <objectsReplicatedAfterVerification>0</objectsReplicatedAfterVerification>\
    </statistics>\
    <status>GOOD</status>\
    <statusMessage>Synchronizing data</statusMessage>\
    <suspended>false</suspended>\
    <type>ACTIVE_ACTIVE</type>\
</link>\
```\
\
## Query parameters for replication link actions\
\
To perform actions on replication links, you use these query parameters:\
\
suspend\
Suspend activity on the link.resume\
Resume activity on a suspended link.failOver\
Fail over the link to the remote system.failBack\
For an active/active link, fail back the link.beginRecover\
For an active/passive link, begin data recovery.completeRecovery\
For an active/passive link, complete data recovery.restore\
Restore the link to the remote system.\
\
You use these query parameters with a POST request against the `link` resource. You cannot include a request body with this request.\
\
Here’s a sample POST request that suspends activity on the replication link named MA-CA:\
\
```\
curl -k -iX POST\
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"\
    "https://admin.hcp-ma.example.com:9090/mapi/services/replication/links/MA-CA\
         ?suspend"\
```\
\
### local (data type for replication link failoverSettings local property)\
\
The `local` data type in this section describes the `local` property of the `failoverSettings` data type that describes the `failoverSettings` property of the `replication link` resource.\
\
## Replication link failoverSettings local data type properties\
\
The following table describes the properties included in the `local` data type that describes the `local` property of the `failoverSettings` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| autoCompleteRecovery | Boolean | For an active/passive link, specifies whether the complete recovery phase starts automatically while data is being recovered from the local system to the remote system for the replication link. Valid values are:<br>true<br> The complete recovery phase starts automatically.false<br> The complete recovery phase does not start automatically.<br>The default is false. | This property is optional on a PUT request to create an active/passive link. It is not valid on a PUT or POST request for an active/active link and is not returned by any GET request for an active/active link. |\
| autoCompleteRecovery<br>Minutes | Integer | For an active/passive link, specifies the number of minutes the up-to-date-as-of time for the replication link must be less than before HCP automatically starts the complete recovery phase while data is being recovered from the local system to the remote system for the link. Valid values are integers in the range one through 9,999. The default is 120.<br>The up-to-date-as-of time is the difference between:<br>- The date and time before which configuration changes and changes to namespace content are guaranteed to have been sent to the remote system<br>- The current date and time | This property is optional on a PUT request to create an active/passive link. It is not valid on a PUT or POST request for an active/active link and is not returned by any GET request for an active/active link. |\
| autoFailover | Boolean | Specifies whether the replication link automatically fails over to the local system after a loss of connectivity to the remote system for the link. Valid values are:<br>true<br> The link automatically fails over.false<br> The link does not fail over automatically.<br>The default is false. | This property is optional on a PUT request to create a replication link. |\
| autoFailoverMinutes | Integer | Specifies the number of minutes HCP waits before automatically failing over the replication link to the local system after a loss of connectivity to the remote system for the link. Valid values are integers in the range seven through 9,999. The default is 120. | This property is optional on a PUT request to create a replication link. |\
\
## Example\
\
Here’s an XML example of the `local` data type that describes the `local` property of the `failoverSettings` data type; the properties shown are those that are returned by a GET request sent to the replica for an active/passive link:\
\
```\
<local>\
    <autoFailover>true</autoFailover>\
    <autoFailoverMinutes>120</autoFailoverMinutes>\
    <autoCompleteRecovery>false</autoCompleteRecovery>\
    <autoCompleteRecoveryMinutes>60</autoCompleteRecoveryMinutes>\
</local>\
```\
\
### local (data type for replication link schedule local property)\
\
The `local` data type in this section describes the `local` property of the `schedule` data type that describes the `schedule` resource for replication links.\
\
## Replication link schedule local data type properties\
\
The following table describes the properties included in the `local` data type that describes the `local` property of the `schedule` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| scheduleOverride | String | Specifies an override for the local schedule for the replication link. Valid values are:<br>low<br> The performance level is low for the entire week.MEDIUM<br> The performance level is medium for the entire week.HIGH<br> The performance level is high for the entire week.CUSTOM<br> The performance level is the custom setting for the entire week.NONE<br> The schedule for the link is not overridden.<br>These values are not case-sensitive.<br>To remove an existing override, specify NONE as the value for this property. If you don’t explicitly remove an existing override when changing the local schedule, the override remains in effect. |  |\
| transition | transition | Specifies a scheduled change of performance level for the replication link on the local system. |  |\
\
## Example\
\
Here’s an XML example of the `local` data type that describes that describes the `local` property of the `schedule` data type:\
\
```\
<local>\
    <scheduleOverride>NONE</scheduleOverride>\
    <transition>\
         <time>Sun:00</time>\
         <performanceLevel>HIGH</performanceLevel>\
    </transition>\
    <transition>\
         <time>Sun:06</time>\
         <performanceLevel>MEDIUM</performanceLevel>\
    </transition>\
    <transition>\
        <time>Sat:00</time>\
        <performanceLevel>HIGH</performanceLevel>\
    </transition>\
    <transition>\
        <time>Sat:06</time>\
        <performanceLevel>MEDIUM</performanceLevel>\
    </transition>\
</local>\
```\
\
### logDownload\
\
The `logDownload` data type describes the download of all system logs for the HCP system.\
\
## logDownload data type properties\
\
The following table describes the properties included in the `logDownload` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| nodes | String | Specifies, through a comma-separated list, the General Nodes in the HCP system you want to download logs from. | If you omit the nodes property from your XML file, you download the logs for all General nodes in the system. If you include the nodes property in your XML file with an empty list, you do not download any logs from the General Nodes. |\
| snodes | String | Specifies, through a comma-separated list, the HCP S Series Nodes in the HCP system you want to download logs for. | If you omit the snodes property from your XML file or include the property with an empty list, you do not download any S Series Node logs. |\
| content | String | Specifies, through a comma-separated list, the General Nodes log types you want to download. The possible types are:<br>- ACCESS<br>- SYSTEM<br>- SERVICE<br>- APPLICATION | The content property only applies to General node. You download all HCP S Series Node log types, regardless of what you specify for the content property.<br>If you omit the content property from your XML file, you download all General Node log types.<br>You cannot include the content property in your XML file with an empty list. |\
\
## Example\
\
Here's an XML example of the `logDownload` data type:\
\
```\
<logDownload>\
    <nodes>175,176</nodes>\
    <snodes>S10-12345,S30-84022</snodes>\
    <content>ACCESS,SYSTEM,SERVICE,APPLICATION</content>\
</logDownload>\
```\
\
### logDownloadPrepare\
\
The `logDownloadPrepare` data type specifies the HCP system logs you want to prepare for download.\
\
## logDownloadPrepare data type properties\
\
The following table describes the properties included in the `logDownloadPrepare` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| startDate | String | Specifies the start date for HCP to begin collecting HCP General Node logs for download preparation | The default is the current date. The date format is:<br>MM/DD/YYYY |\
| endDate | String | Specifies the end date for HCP to finish collecting HCP General Node logs for download preparation | The default is the current date. The date format is:<br>MM/DD/YYYY |\
| snodes | String | Specifies, through a comma-separated list, the HCP S Series Nodes that you want to prepare HCP system logs for download |  |\
\
## Example\
\
Here's an XML example of the `logDownloadPrepare` data type:\
\
```\
<logDownloadPrepare>\
    <startDate>06/14/2017</startDate>\
    <endDate>07/14/2017</endDate>\
    <snodes>S10-12345, S30-28843</snodes>\
</logDownloadPrepare>\
```\
\
### logDownloadStatus\
\
The `logDownloadStatus` data type describes the download status for General and S Series Nodes.\
\
## logDownloadStatus data type properties\
\
The following table describes the properties included in the `logDownloadStatus` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| readyForStreaming | Boolean | Specifies whether the download package is ready to be downloaded. Valid values are:<br>true<br> The logs have been prepared are ready for download.false<br> The logs have either not been prepared, are still being prepared, or they have not been downloaded for twenty four hours after being prepared and so have been deleted. |  |\
| streamingInProgress | Boolean | Specifies whether the download package is currently being downloaded. Valid values are:<br>true<br> The logs are currently being downloaded.false<br> The logs are not currently being downloaded. |  |\
| started | Boolean | Specifies whether the download package is currently being prepared. Valid values are:<br>true<br> The logs are currently being prepared for download.false<br> The logs are not currently being prepared for downloaded. |  |\
| error | Boolean | Specifies whether an error has occurred during the package preparation or download phase. Valid values are:<br>true<br> The logs are experiencing an error with being prepared or downloaded.false<br> The logs are not experiencing an error with being prepared or downloaded. |  |\
| content | String | Specifies, through a comma-separated list, the General Nodes log types being downloaded. The possible types are:<br>- ACCESS<br>- SYSTEM<br>- SERVICE<br>- APPLICATION | This list does not apply to HCP S Series Nodes. All list types are taken from HCP S Series Nodes regardless of the content. |\
\
## Example\
\
Here's an XML example of the `logDownloadStatus` data type:\
\
```\
<logDownloadStatus>\
    <readyForStreaming>true</readyForStreaming>\
    <streamingInProgress>false</streamingInProgress>\
    <started>true</started>\
    <error>false</error>\
    <content>ACCESS,SYSTEM,SERVICE,APPLICATION</content>\
</logDownloadStatus>\
```\
\
### namespace\
\
The `namespace` data type describes the `namespaces` resource.\
\
## Properties\
\
The table below describes the properties included in the `namespace` data type.\
\
| Property | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| aclsUsage | String | Specifies whether the namespace supports access control lists (ACLs) and, if so, whether HCP honors ACLs in the namespace. Valid values are:<br> NOT\_ENABLED<br> The namespace does not support ACLs.ENFORCED<br> <br> The namespace supports ACLs, and HCP honors ACLs in the namespace.<br> NOT\_ENFORCED<br> <br> The namespace supports ACLs, but HCP does not honor ACLs in the namespace.<br> <br>The default is NOT\_ENABLED.<br>These values are not case sensitive. | This property is optional on a PUT request.<br>You can change the value of this property from NOT\_ENABLED to ENFORCED or NOT\_ENFORCED but not from ENFORCED or NOT\_ENFORCED to NOT\_ENABLED.<br>This property is not valid for the default namespace. |\
| allowErasureCoding | Boolean | For an HCP namespace, specifies whether HCP is allowed to use erasure coding to implement replication of the objects in the namespace. Valid values are:<br>true<br> HCP is allowed to use erasure coding for replication of the namespace.<br> false<br> HCP is not allowed to use erasure coding for replication of the namespace.<br> <br>If the tenant is allowed to manage erasure coding for namespaces, the default is the setting specified by the namespace defaults for the tenant. If the tenant does not have this ability, the default is to allow erasure coding for namespaces that are cloud optimized. | This property is valid on a PUT or POST request only if the owning tenant is allowed to manage erasure coding for its namespaces.<br>This property is optional on a PUT request.<br>You can set the value of this property to `true` on a PUT request only if the value of the optimizedFor property is set to `true` in the same request or if new namespaces are cloud optimized by default.<br>You can set the value of this property to `true` on a POST request only if the namespace is already cloud optimized or the optimizedFor property is set to `true` in the same request.<br>This property is not returned by any GET request for a namespace that is not selected for replication or for which the owning tenant is not allowed to manage erasure coding for its namespaces.<br>This property is not valid for the default namespace. |\
| allowPermissionAnd OwnershipChanges | Boolean | Specifies whether changes to POSIX UIDs, GIDs, and permissions and to object ownership are allowed for objects under retention in the namespace. Valid value are:<br>true<br> Changes to POSIX UIDs, GIDs, and permissions and to object ownership are allowed for objects under retention.false<br> Changes to POSIX UIDs, GIDs, and permissions and to object ownership are not allowed for objects under retention.<br>The default is `false`. | This property is optional on a PUT request.<br>This property is not valid for the default namespace. |\
| appendEnabled | Boolean | Specifies whether the namespace supports appendable objects. Valid values are:<br>true<br> The namespace supports appendable objects.false<br> The namespace does not support appendable objects.<br>The default is false. | This property is optional on a PUT request.<br>You cannot enable both appendable objects and versioning for a namespace.<br>This property is not valid for the default namespace.<br>**Note:** The appendable-object feature provides snaplock volume support for third-party backup solutions. Do not enable this feature unless you have been explicitly told to do so. |\
| atimeSynchronization Enabled | Boolean | Specifies whether atime synchronization with retention settings is enabled for the namespace. Valid values are:<br>true<br> Atime synchronization is enabled.false<br> Atime synchronization is not enabled.<br>The default is false. | This property is optional on a PUT request.<br>This property is not valid for the default namespace. |\
| authAndAnonymous MinimumPermissions | List | Lists the minimum data access permissions for all unauthenticated users of the namespace. Authenticated users also have these permissions when the value of the `authUsersAlways-GrantedAllPermissions` property is `true`.<br>Valid values for permissions are:<br>- BROWSE<br>- DELETE<br>- PURGE<br>- READ<br>- READ\_ACL<br>- WRITE<br>- WRITE\_ACL<br>The default is no permissions.<br>These values are not case sensitive. | This property is optional on a PUT request.<br>The set of permissions specified in the request body replaces the current set of minimum permissions for all users. To remove all the permissions for all users, specify an empty list.<br>If the set of permissions includes PURGE, delete permission is enabled automatically. If the set of permissions includes READ, browse permission is enabled automatically.<br>This property is not valid for the default namespace. |\
| authMinimum Permissions | List | Lists the minimum data access permissions for authenticated users of the namespace. Valid values for permissions are:<br>- BROWSE<br>- DELETE<br>- PURGE<br>- READ<br>- READ\_ACL<br>- WRITE<br>- WRITE\_ACL<br>The default is no permissions.<br>These values are not case sensitive. | This property is optional on a PUT request.<br>The set of permissions specified in the request body replaces the current set of permissions for authenticated users. To remove all the permissions for authenticated users, specify an empty list.<br>If the set of permissions includes PURGE, delete permission is enabled automatically. If the set of permissions includes READ, browse permission is enabled automatically.<br>This property is not valid for the default namespace. |\
| authUsersAlwaysGranted AllPermissions | Boolean | Specifies whether users have the minimum data access permissions specified by the `authAndAnonymous-MinimumPermissions` property in addition to the minimum data access permissions specified by the `authMinimumPermissions` property when using a protocol that requires authentication. Valid values are:<br>true<br> Users have the minimum data access permissions specified by both properties when using a protocol that requires authentication.false<br> <br> Users have only the minimum data access permissions specified by the `authMinimum-Permissions` property when using a protocol that requires authentication.<br> <br>The default is true. | This property is optional on a PUT request.<br>This property is not valid for the default namespace. |\
| creationTime | String | Specifies the date and time at which the namespace was created, in this ISO 8601 format:<br>`yyyy-MM-ddThh:mm:ssZ`<br>`Z` represents the offset from UTC, in this format:<br>( `+` \| `-`) `hhmm`<br>For example:<br>2017-02-09T19:44:03-0500 | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request. |\
| customMetadata IndexingEnabled | Boolean | Specifies whether custom metadata indexing is enabled for the namespace. Valid values are:<br>true<br> Custom metadata indexing is enabled.false<br> Custom metadata indexing is disabled.<br>The default is false. | This property is optional on a PUT request.<br>You can set this property to `true` only while indexing is enabled for the namespace.<br>Disabling custom metadata indexing for the namespace automatically disables full custom metadata indexing. |\
| customMetadata ValidationEnabled | Boolean | Specifies whether custom metadata XML checking is enabled for the namespace. Valid values are:<br>true<br> <br> When custom metadata is added to an object in the namespace, HCP checks whether the custom metadata is well-formed XML.<br> false<br> <br> When custom metadata is added to an object in the namespace, HCP does not check whether the custom metadata is well-formed XML.<br> <br>The default is false. | This property is optional on a PUT request. |\
| description | String | Specifies a description of the namespace. This description is optional. The default is the description specified by the namespace defaults for the tenant.<br>To remove a description from an existing namespace, specify the description property with no value. | This property is optional on a PUT request. |\
| directoryUsage | String | Specifies whether the cloud optimized namespace is configured for balanced directory mode or unbalanced directory mode. Valid values are:<br>Balanced<br> The directory usage is configured for balanced mode. A well distributed directory structure is required to avoid hot spotting and ensure optimal performance.Unbalanced<br> The directory usage is configured for unbalanced mode. Optimal performance is not dependent upon a directory structure.<br>The default is specified by the namespace defaults for the tenant. These values are not case sensitive. |  |\
| dpl | String |  | Deprecated. Namespace DPL is now a service plan function.<br>This property is ignored on a PUT or POST request. In response to a GET request, the value of this property is always `Dynamic`. |\
| enterpriseMode | Boolean | Specifies the namespace retention mode. Valid values are:<br>true<br> The namespace is in enterprise mode.false<br> The namespace is in compliance mode.<br>The default is the retention mode specified by the namespace defaults for the tenant. | This property is optional on a PUT request.<br>You can set the value of this property to `false` only if the owning tenant is allowed to set the retention mode for its namespaces.<br>In enterprise mode, privileged deletes are allowed, retention class durations can be shortened, and retention classes can be deleted. In compliance mode, these activities are not allowed.<br>The value of this property can be changed from `true` to `false` but not from `false` to `true`. |\
| fullyQualifiedName | String | Specifies the fully qualified hostname of the namespace. | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request. |\
| hardQuota | String | For an HCP namespace, specifies the total amount of space allocated to the namespace. This is the space available for storing objects, including object data, metadata, and the redundant data required to satisfy the namespace service plan.<br>Valid values are decimal numbers with up to two places after the period, followed by a space, followed by MB, GB, or TB (for example, 1.25 TB). The minimum value is 1 (one) for GB and .01 for TB. The maximum value is the amount of space the tenant has available to allocate to its namespaces. The default is the hard quota specified by the namespace defaults for the tenant. | This property is optional on a PUT request.<br>This property is not valid for the default namespace. |\
| hashScheme | String | Specifies the cryptographic hash algorithm for the namespace. Valid values are:<br>- MD5<br>- SHA-1<br>- SHA-256<br>- SHA-384<br>- SHA-512<br>- RIPEMD-160<br>These values are case sensitive.<br>The default is the cryptographic hash algorithm specified by the namespace defaults for the tenant. | This property is optional on a PUT request. It is not valid on a POST request and is returned only by a verbose GET request. |\
| id | String | Specifies the system-supplied unique ID for the namespace. HCP generates this ID automatically when you create a namespace. | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request. |\
| indexingDefault | Boolean | For an HCP namespace, specifies the default index setting for objects added to the namespaces. Valid values are:<br>true<br> New objects are marked for indexing.false<br> New objects are not marked for indexing.<br>The default is true. | This property is optional on a PUT request.<br>This property is not valid for the default namespace. |\
| indexingEnabled | Boolean | Specifies whether indexing is enabled for the namespace. Valid values are:<br>true<br> Indexing is enabled.false<br> Indexing is disabled.<br>On a PUT request, the default is `true` if the searchEnabled property is set to `true` in the same request; otherwise, the default is `false`. | This property is optional on a PUT request.<br>You can set this property to `true` only while search is enabled for the namespace.<br>Disabling indexing for the namespace automatically disables custom metadata indexing. |\
| isDplDynamic | Boolean |  | Deprecated. Namespace DPL is now a service plan function.<br>This property is not valid on a PUT or POST request. In response to a GET request, the value of this property is always `true`. |\
| mqeIndexingTimestamp | String | Specifies the date and time before which objects are guaranteed to have been indexed by the metadata query engine, in this ISO 8601 format:<br>`yyyy-MM-ddThh:mm:ssZ`<br>`Z` represents the offset from UTC, in this format:<br>( `+` \| `-`) `hhmm`<br>For example:<br>2017-02-09T19:44:03-0500 | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request. |\
| multipartUploadAuto AbortDays | Integer | For an HCP namespace, specifies the number of days multipart uploads can remain incomplete before they are automatically aborted. Valid values are integers in the range zero through 180. The default is 30.<br>A value of zero means multipart uploads are never automatically aborted. | This property is optional on a PUT request.<br>This property is not valid for the default namespace. |\
| name | String | Specifies the name of the namespace. HCP derives the hostname for the namespace from this name. The hostname is used in URLs for access to the namespace.<br>In English, the name you specify for a namespace must be from one through 63 characters long and can contain only alphanumeric characters and hyphens (-) but cannot start or end with a hyphen. In other languages, because the derived hostname cannot be more than 63 characters long, the name you specify may be limited to fewer than 63 characters.<br>Namespace names cannot contain special characters other than hyphens and are not case sensitive. White space is not allowed.<br>Namespace names cannot start with xn-- (that is, the characters x and n followed by two hyphens).<br>Namespace names must be unique for the owning tenant. Different tenants can have namespaces with the same name.<br>You can reuse namespace names that are not currently in use. So, for example, if you delete a namespace, you can give a new namespace the same name as the one the deleted namespace had.<br>The name of the default namespace is always Default. | This property is required on a PUT request.<br>The namespace name is used in the URL for access to the namespace.<br>You can change the namespace name any time after you create the namespace, except while the S3 compatible, CIFS, or NFS protocol is enabled for the namespace. However, when you change the name, the URL for the namespace may change as well. |\
| optimizedFor | String | For an HCP namespace, specifies whether the namespace is optimized for cloud protocols only or optimized for all namespace access protocols. Valid values are:<br>CLOUD<br> Optimizes the namespace for cloud protocols onlyALL<br> Optimizes the namespace for all namespace access protocols<br>The default is specified by the namespace defaults for the tenant.<br>These values are not case sensitive. | This property is optional on a PUT request.<br>You can set this property to `ALL` on a POST request only if the namespace already does not allow erasure coding or the `allowErasureCoding` property is set to `false` in the same request.<br>This property is not valid for the default namespace |\
| owner | String | For an HCP namespace, specifies the namespace owner. Valid values are:<br>- If the owner has an HCP user account, the username for that account.<br>- If the owner has an AD user account, the account username along with the name of the AD domain in which the account is defined, in this format: username@ad-domain-name<br>If the namespace doesn’t have an owner, this property has no value. | This property is optional on a PUT request.<br>If this property is included on a PUT or POST request with a value that identifies an AD user account, the request must also include the ownerType property with a value of `EXTERNAL`.<br>In response to a GET request, if the owner is an AD user and HCP cannot communicate with AD or cannot find the user account in AD, the value of this property is the security ID (SID) for the AD user account.<br>This property is not valid for the default namespace. |\
| ownerType | String | For an HCP namespace, specifies the type of the user account that identifies the object owner. Valid values are:<br>LOCAL<br> <br> The user account is defined in HCP.<br> EXTERNAL<br> The user account is defined in AD.<br>The default is `LOCAL`.<br>These values are not case sensitive.<br>If the namespace doesn’t have an owner, this property has no value. | This property is optional on a PUT request.<br>You can specify a value for this property on a PUT or POST request only if you specify a value for the owner property in the same request.<br>This property is not valid for the default namespace. |\
| readFromReplica | Boolean | Specifies whether read from replica is enabled for the namespace.<br>Valid values are:<br>• `true` — Read from replica is enabled.<br>• `false` — Read from replica is disabled.<br>On a `PUT` request, the default is `true` if replicationEnabled is set to `true` in the same request; otherwise, the default is `false`. | This property is optional on a PUT request. You can set the value of this property to `true` on a PUT request only if, for an HCP namespace, the replicationEnabled property is set to `true` in the same request or, for the default namespace, the HCP system supports replication.<br>On a POST request, you can set the value for this property to `true` for an HCP namespace only if the namespace is already selected for replication or the replicationEnabled property is set to `true` in the same request. You can set the value for this property to `true` for the default namespace only if the HCP system supports replication.<br>This property is not returned by any GET request for an HCP namespace that is not selected for replication. This property is not returned by any GET request for the default namespace if the HCP system does not support replication. |\
| replicationEnabled | Boolean | For an HCP namespace, specifies whether the namespace is selected for replication. Valid values are:<br>• `true` — The namespace is selected for replication.<br>• `false` — The namespace is not selected for replication.<br>The default is the replication setting specified by the namespace defaults for the tenant. | This property is optional on a `PUT` request.<br>You can set the value of this property to `true` only if replication is enabled for the owning tenant.<br>If the HCP system does not support replication, this property is not returned by any `GET` request.<br>This property is not valid for the default namespace. If the HCP system supports replication, replication is automatically enabled for that namespace. |\
| replicationTimestamp | String | Specifies the oldest change time for objects and configuration changes not yet replicated or recovered from the namespace, as applicable, in this format:<br>`yyyy-MM-ddThh:mm:ssZ`<br>`Z` represents the offset from UTC, in this format:<br>( `+` \| `-`) `hhmm`<br>For example:<br>2012-02-27T14:32:19-0500 | This property is not valid on a `PUT` or `POST` request. It is returned only by a verbose `GET` request and only when these conditions are true:<br>•The namespace is selected for replication.<br>•The tenant that owns the namespace is included in a replication link. |\
| retentionType | String | Specifies the retention type to be applied to all objects in the namespace. Valid values are:<br> HCP<br> HCP retention type<br> S3<br> S3 Object Lock<br>Default value is HCP retention. | When you set the retentionType to S3 Object Lock in a MAPI request for a namespace, versioning, delete markers, and cloud-optimized protocols are automatically enabled. You do not need to set these parameters explicitly. |\
| s3UnversionedOverwrite | Boolean | Specifies whether object overwrite is enabled for the namespace. Valid values are:<br>true<br> Object overwrite is enabled.false<br> Object overwrite is disabled.<br>The default is false. | You can set the value of this property to either `true` or `false`. However, if you enable versioning for the namespace, changing the value will not have any effect. This property applies only to the requests made through Hitachi API for Amazon S3.<br>Overwriting an object will also overwrite its custom metadata. |\
| searchEnabled | Boolean | Specifies whether search is enabled for the namespace. Valid values are:<br>true<br> Search is enabled.false<br> Search is disabled.<br>The default is the search setting specified by the namespace defaults for the tenant. | This property is optional on a `PUT` request.<br>You can set the value of this property to `true` only if the owning tenant is allowed to enable search for its namespaces.<br>Disabling search after it has been enabled:<br>- Automatically disables indexing for the namespace.<br>- Removes objects in the namespace from the metadata query engine index. If you subsequently reenable search for the namespace, the namespace must be reindexed from scratch.<br>- Deletes the list of excluded annotations for the namespace.<br>- Deletes all associations the namespace has with any content classes. |\
| servicePlan | String | Specifies the service plan associated with the namespace. Valid values are names of existing service plans. The default is the service plan specified by the namespace defaults for the tenant. | This property is valid on a PUT or POST request and returned by a GET request only if the owning tenant is allowed to select service plans for its namespaces. If valid, this property is optional on a PUT request. |\
| serviceRemoteSystemRequests | Boolean | Specifies whether HCP can service HTTP-based data access requests against the namespace that are redirected from other HCP systems. Valid values are:<br>true<br> HCP can service requests against the namespace that are redirected from other systems.<br> false<br> HCP cannot service requests against the namespace that are redirected from other systems.<br> <br>The default is true. | This property is optional on a PUT request. |\
| softQuota | Integer | For an HCP namespace, specifies the percentage point at which HCP notifies the tenant that free storage space for the namespace is running low. Valid values are integers in the range ten through 95. The default is the soft quota specified by the namespace defaults for the tenant. | This property is optional on a PUT request.<br>This property is not valid for the default namespace. |\
| tags | List | Associates zero, one, or more tags with the namespace. Each tag can be from one through 64 characters long and can contain any valid UTF-8 characters except commas (,). White space is allowed.<br>Tags are not case sensitive. | This property is optional on a PUT request.<br>For an existing namespace, the set of tags specified in the request body replaces the set of tags currently associated with the namespace. To remove all tags, specify an empty set.<br>In XML, the element that identifies each tag is `tag`. In JSON, the name in the name/value pair that lists the tags is `tag`.<br>This property is not valid for the default namespace. |\
| versioningSettings | versioning<br>Settings | For an HCP namespace, specifies the versioning settings for the namespace.<br>If this property is omitted on a PUT request, the default is the versioning settings specified by the namespace defaults for the tenant. | This property is optional on a PUT request. If this property is included on a PUT request and the owning tenant is not allowed to set versioning for its namespaces, the value of the enabled property for versioningSettings, if specified, must be `false`.<br>This property is not valid on a POST request and is not returned by a GET request. To modify or retrieve the versioning settings for a namespace, use the namespace versioning-Settings resource.<br>This property is not valid for the default namespace.<br>You cannot enable versioning for a namespace while the CIFS, NFS, WebDAV, or SMTP protocol or appendable objects are enabled. |\
\
## Query parameter for restarting indexing\
\
While search is enabled for a namespace, you can restart metadata query engine indexing of the namespace from the time the namespace was created or from a specified date. To do this, you use this query parameter:\
\
```\
resetMQECheckpoint=(mm/dd/yyyy|0)\
```\
\
The value 0 (zero) restarts indexing from the time the namespace was created.\
\
You use the resetMQECheckpoint query parameter with a POST request against the namespace resource.\
\
Here’s a sample POST request that restarts metadata query engine indexing of the accounts-receivable namespace from the time the namespace was created:\
\
```\
curl -k -i -d "<namespace/>" -H "Content-Type: application/xml"\
    -H "Authorization: HCP bGdyZWVu:a3b9c163f6c520407ff34cfdb83ca5c6"\
    "https://finance.hcp.example.com:9090/mapi/tenants/finance/namespaces/\
     accounts-receivable?resetMQECheckpoint=0”\
```\
\
## Example\
\
Here’s an XML example of the `namespace` data type; the properties shown are those that are returned in response to a verbose GET request:\
\
```\
<namespace>\
    <aclsUsage>ENFORCED</aclsUsage>\
    <authUsersAlwaysGrantedAllPermissions>true\
    </authUsersAlwaysGrantedAllPermissions>\
    <allowPermissionAndOwnershipChanges>true\
    </allowPermissionAndOwnershipChanges>\
    <appendEnabled>false</appendEnabled>\
    <atimeSynchronizationEnabled>false</atimeSynchronizationEnabled>\
    <authMinimumPermissions>\
        <permission>BROWSE</permission>\
        <permission>READ</permission>\
        <permission>WRITE</permission>\
    </authMinimumPermissions>\
    <creationTime>2017-02-09T15:42:36-0500</creationTime>\
    <customMetadataIndexingEnabled>true</customMetadataIndexingEnabled>\
    <customMetadataValidationEnabled>true</customMetadataValidationEnabled>\
    <description>Created for the Finance department at Example Company by Lee\
        Green on 2/9/2017.</description>\
    <dpl>Dynamic</dpl>\
    <enterpriseMode>true</enterpriseMode>\
    <allowErasureCoding>true</allowErasureCoding>\
    <fullyQualifiedName>Accounts-Receivable.Finance.hcp.example.com\
    </fullyQualifiedName>\
    <hardQuota>50 GB</hardQuota>\
    <hashScheme>SHA-256</hashScheme>\
    <indexingDefault>true</indexingDefault>\
    <indexingEnabled>true</indexingEnabled>\
    <isDplDynamic>true</isDplDynamic>\
    <mqeIndexingTimestamp>2017-02-26T18:11:13-0400</mqeIndexingTimestamp>\
    <multipartUploadAutoAbortDays>30</multipartUploadAutoAbortDays>\
    <name>Accounts-Receivable</name>\
    <optimizedFor>CLOUD</optimizedFor>\
    <owner>pblack</owner>\
    <ownerType>LOCAL</ownerType>\
    <readFromReplica>true</readFromReplica>\
    <replicationEnabled>true</replicationEnabled>\
    <replicationTimestamp>2017-02-27T06:45:52-0500</replicationTimestamp>\
    <searchEnabled>true</searchEnabled>\
    <servicePlan>Short-Term-Activity</servicePlan>\
    <serviceRemoteSystemRequests>true</serviceRemoteSystemRequests>\
    <softQuota>75</softQuota>\
    <s3UnversionedOverwrite>false</s3UnversionedOverwrite>\
    <tags>\
        <tag>Billing</tag>\
        <tag>lgreen</tag>\
    </tags>\
    <id>0e774b8d-8936-4df4-a352-b68766b5c287</id>\
    <authAndAnonymousMinimumPermissions>\
        <permission>BROWSE</permission>\
        <permission>READ</permission>\
    </authAndAnonymousMinimumPermissions>\
</namespace>\
```\
\
### namespaceDefaults\
\
The `namespaceDefaults` data type describes the `namespaceDefaults` resource.\
\
## Properties\
\
The table below describes the properties included in the `namespaceDefaults` data type.\
\
| Property | Date Type | Description | Notes |\
| --- | --- | --- | --- |\
| allowErasureCoding | Boolean | Specifies the default setting for whether new namespaces allow erasure coding. Valid values are:<br>true<br> The namespace allows erasure coding.false<br> The namespace does not allow erasure coding.<br>The default is false. | This property is valid on a POST request only if the tenant for which you’re specifying namespace defaults is allowed to manage erasure coding for its namespaces.<br>You can set the value of this property to `true` on a POST request only if cloud optimization is already set as the default for new namespaces or if the optimizedFor property is set to `true` in the same request.<br>This property is returned by a GET request only if the tenant for which you’re specifying namespace defaults is allowed to manage erasure coding for its namespaces. |\
| description | String | Specifies the default description for new HCP namespaces. This description is optional. The default is no description.<br>To remove a description from the namespace defaults, specify the description property with no value. |  |\
| directoryUsage | String | Specifies whether new cloud-optimized namespaces are balanced/unbalanced by default. Valid values are:<br>Balanced<br> The directory usage is configured for balanced mode. A well distributed directory structure is required to avoid hot spotting and ensure optimal performance.Unbalanced<br> The directory usage is configured for unbalanced mode. Optimal performance is not dependent upon a directory structure.Default<br> The directory usage setting for the namespace is determined by the system-level setting for this property.<br>The default value is Default. The values are not case sensitive. |  |\
| dpl | String |  | Deprecated. Namespace DPL is now a service plan function.<br>This property is ignored on a POST request. In response to a GET request, the value of this property is always `Dynamic`. |\
| effectiveDpl | String |  | Deprecated. Namespace DPL is now a service plan function.<br>This property is not valid on a POST request and is returned only by a verbose GET request. In response to a verbose GET request, the value of this property is always `Dynamic`. |\
| enterpriseMode | Boolean | Specifies the default retention mode for new HCP namespaces. Valid values are:<br>true<br> The namespace is in enterprise mode.false<br> The namespace is in compliance mode.<br>The default is true. | In enterprise mode, privileged deletes are allowed, retention class durations can be shortened, and retention classes can be deleted. In compliance mode, these activities are not allowed.<br>This property is valid on a POST request and returned by a GET request only if the tenant for which you’re specifying namespace defaults is allowed to set the retention mode for its namespaces. |\
| hardQuota | String | Specifies the default hard quota for new HCP namespaces.<br>The hard quota is the total amount of space allocated to the namespace. This is the space available for storing objects, including object data, metadata, and the redundant data required to satisfy the namespace service plan.<br>Valid values are decimal numbers with up to two places after the period, followed by a space, followed by MB, GB, or TB (for example, 1.25 TB). The minimum value is 1 (one) for GB and .01 for TB. The maximum value is equal to the hard quota for the tenant. The default is 50 GB. |  |\
| hashScheme | String | Specifies the default cryptographic hash algorithm for new HCP namespaces. Valid values are:<br>- MD5<br>- SHA-1<br>- SHA-256<br>- SHA-384<br>- SHA-512<br>- RIPEMD-160<br>These values are case sensitive.<br>The default is SHA-256. |  |\
| multipartUploadAuto AbortDays | Long | Specifies the default number of days multipart uploads can remain incomplete before they are automatically aborted. Valid values are integers in the range zero through 180. The default is 30.<br>A value of zero means multipart uploads are never automatically aborted. |  |\
| retentionType | String | Specifies the retention type to be applied to all objects in the namespace. Valid values are:<br> HCP<br> HCP retention type<br> S3<br> S3 Object Lock<br>Default value is HCP retention. | When you set the retentionType to S3 Object Lock in a MAPI request for a namespace, versioning, delete markers, and cloud-optimized protocols are automatically enabled. You do not need to set these parameters explicitly. |\
| optimizedFor | String | Specifies whether new namespaces are cloud optimized by default. Valid values are:<br>ALL<br> The namespace is optimized for all namespace access protocols.CLOUD<br> The namespace is optimized for cloud protocols only.DEFAULT<br> The protocol optimization setting for the namespace is determined by the system-level setting for this property.<br>The default is DEFAULT.<br>These values are not case sensitive. | You can set this property to `ALL` or `DEFAULT` on a POST request only if new namespaces already disallow erasure coding by default or the allowErasureCoding property is set to `false` in the same request. |\
| replicationEnabled | Boolean | Specifies the default replication setting for new HCP namespaces. Valid values are:<br>true<br> The namespace is selected for replication.false<br> The namespace is not selected for replication.<br>The default is false. | This property is valid on a POST request and returned by a GET request only if replication is enabled for the tenant for which you’re specifying namespace defaults. |\
| s3UnversionedOverwrite | Boolean | Specifies whether object overwrite is enabled for the namespace. Valid values are:<br>true<br> Object overwrite is enabled.false<br> Object overwrite is disabled.<br>The default is false. | You can set the value of this property to either `true` or `false`. However, if you enable versioning for the namespace, changing the value will not have any effect. This property applies only to the requests made through Hitachi API for Amazon S3.<br>Overwriting an object will also overwrite its custom metadata. |\
| searchEnabled | Boolean | Specifies the default search setting for new HCP namespaces. Valid values are:<br>true<br> Search is enabled.false<br> Search is disabled.<br>The default is false. | This property is valid on a POST request and returned by a GET request only if the owning tenant is allowed to enable search for its namespaces. |\
| servicePlan | String | Specifies the default service plan for new HCP namespaces. Valid values are names of existing service plans. The default is the Default service plan. | This property is valid on a POST request and returned by a GET request only if the tenant for which you’re specifying namespace defaults is allowed to select service plans for its namespaces. |\
| softQuota | Integer | Specifies the default soft quota for new HCP namespaces. Valid values are integers in the range ten through 95. The default is 85. |  |\
| versioningSettings | versioning<br>Settings | Specifies the default versioning settings for new HCP namespaces.<br>The default is no versioning. | This property is valid on a POST request and returned by a GET request only if the tenant for which you’re specifying namespace defaults has versioning configuration enabled. |\
\
## Example\
\
Here’s an XML example of the `namespaceDefaults` data type:\
\
```\
<namespaceDefaults>\
    <description></description>\
    <dpl>Dynamic</dpl>\
    <effectiveDpl>Dynamic</effectiveDpl>\
    <enterpriseMode>true</enterpriseMode>\
    <allowErasureCoding>true</allowErasureCoding>\
    <hardQuota>75 GB</hardQuota>\
    <hashScheme>SHA-256</hashScheme>\
    <optimizedFor>CLOUD</optimizedFor>\
    <replicationEnabled>true</replicationEnabled>\
    <searchEnabled>true</searchEnabled>\
    <servicePlan>Short-Term-Activity</servicePlan>\
    <softQuota>75</softQuota>\
    <versioningSettings>\
         <enabled>true</enabled>\
         <prune>true</prune>\
         <pruneDays>10</pruneDays>\
         <useDeleteMarkers>true</useDeleteMarkers>\
    </versioningSettings>\
</namespaceDefaults>\
```\
\
### namespacePermission\
\
The `namespacePermission` data type describes the `namespacePermission` property of the `dataAccessPermissions` data type.\
\
## Properties\
\
The table below describes the properties included in the `namespacePermission` data type.\
\
| Property | Data Type | Description | Notes |\
| --- | --- | --- | --- |\
| namespaceName | String | Specifies the name of the namespace to which the permissions listed in the permission property provide access. |  |\
| permissions | List | Lists the permissions associated with the namespace identified by the `namespaceName` property. Valid values for permissions are:<br>- BROWSE<br>- CHOWN<br>- DELETE<br>- PRIVILEGED<br>- PURGE<br>- READ<br>- READ\_ACL<br>- SEARCH<br>- WRITE\_ACL<br>- WRITE<br>These values are not case sensitive. | The set of permissions specified in the request body replaces the set of permissions currently associated with the namespace identified by the namespaceName property. To remove all the permissions for a namespace, specify an empty list.<br>If the set of permissions includes PURGE, delete permission is enabled automatically. If the set of permissions includes READ, browse permission is enabled automatically. If the set of permissions includes SEARCH, browse and read permissions are enabled automatically. |\
\
## Example\
\
Here’s an XML example of the `namespacePermission` data type:\
\
```\
<namespacePermission>\
    <namespaceName>Accounts-Receivable</namespaceName>\
    <permissions>\
         <permission>BROWSE</permission>\
         <permission>CHOWN</permission>\
         <permission>SEARCH</permission>\
         <permission>PURGE</permission>\
         <permission>WRITE_ACL</permission>\
         <permission>DELETE</permission>\
         <permission>PRIVILEGED</permission>\
         <permission>READ</permission>\
        <permission>READ_ACL</permission>\
         <permission>WRITE</permission>\
    </permissions>\
</namespacePermission>\
```\
\
### networkSettings\
\
The `network` data type sets the downstream DNS to either basic or advanced mode.\
\
## networkSettings data type properties\
\
The following table describes the properties included in the `network` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| downstreamDNSMode | String | Specifies whether downstream DNS is set to basic or advanced mode. Valid values are:<br>- BASIC<br>- ADVANCED<br>The default is basic. |  |\
\
## Example\
\
Here's an XML example of the `network` data type:\
\
```\
<networkSettings>\
    <downstreamDNSMode>ADVANCED</downstreamDNSMode>\
</networkSettings>\
```\
\
### nfsProtocol\
\
The `nfsProtocol` data type describes the nfs resource for HCPnamespaces.\
\
## Properties\
\
The table below describes the properties included in the `nfsProtocol` data type.\
\
| Property | Data type | Description |\
| --- | --- | --- |\
| enabled | Boolean | Specifies whether the NFS protocol is enabled for the namespace. Valid values are:<br>true<br> NFS is enabled.false<br> NFS is disabled.<br>The default is false. |\
| gid | Integer | Specifies the default POSIX GID for objects that don’t have an explicit POSIX GID. Valid values are integers greater than or equal to zero. The default is zero. |\
| ipSettings | ipSettings | Specifies which IP addresses can access the namespace through the NFS protocol. |\
| uid | Integer | Specifies the default POSIX UID for objects that don’t have an explicit POSIX UID. Valid values are integers greater than or equal to zero. The default is zero. |\
\
## Example\
\
Here’s an XML example of the `nfsProtocol` data type:\
\
```\
<nfsProtocol>\
    <enabled>true</enabled>\
    <gid>0</gid>\
    <ipSettings>\
         <allowAddresses>\
             <ipAddress>192.168.140.10</ipAddress>\
             <ipAddress>192.168.140.14</ipAddress>\
             <ipAddress>192.168.140.15</ipAddress>\
             <ipAddress>192.168.149.0/24</ipAddress>\
         </allowAddresses>\
    </ipSettings>\
    <uid>0</uid>\
</nfsProtocol>\
```\
\
### node ()\
\
### node\
\
### nodes\
\
### nodeStatistics\
\
The `nodeStatistics` data type retrieves information about the statistics of nodes in your HCP system.\
\
## nodeStatistics data type properties\
\
The next table describes the properties included in the `nodeStatistics` data type.\
\
| Property name | Data type | Description |\
| --- | --- | --- |\
| backEndBytesRead | Float | Average number of bytes read from the node per second over the back-end network. |\
| backEndBytesWritten | Float | Average number of bytes written to the node per second over the back-end network. |\
| backendIpAddress | String | Backend IP address. |\
| blocksRead | Float | Average number of blocks read from the logical volume per second. |\
| blocksWritten | Float | Average number of blocks written to the logical volume per second. |\
| collectionTimestamp | Integer | Exact time that the statistics were collected in milliseconds. |\
| cpuMax | Float | Maximum amount of CPU capacity allotted for HCP processes. |\
| cpuSystem | Float | Percentage of CPU capacity used by the operating system kernel. |\
| cpuUser | Float | Percentage of CPU capacity used by HCP processes. |\
| diskUtilization | Float | Use of the communication channel between the operating system and the logical volume as a percent of the channel bandwidth. |\
| freeBytes | Long | Available free space on a HCP volume. |\
| freeInodes | Long | Available free inodes on a HCP volume. |\
| frontEndBytesRead | Float | Average number of bytes read from the node per second over the front-end network. |\
| frontEndBytesWritten | Float | Average number of bytes written to the node per second over the front-end network. |\
| frontendIpAddresses | List | List of front-end IP addresses. This property lists one or two IP addresses (IPv4 and/or IPv6). |\
| id | Float | Name of the logical volume. |\
| ioWait | Float | Percentage of CPU capacity spent waiting to access logical volumes that are in use by other processes. |\
| managementBytesRead | Float | Average number of bytes read from the node per second over the management port network. This is returned only if you have configured the management port network on the node. |\
| managementBytesWritten | Float | Average number of bytes written to the node per second over the management port network. This is returned only if you have configured the management port network on the node. |\
| managementIpAddresses | List | List of management network IP addresses. This is returned only if you have configured the management port network on the node. |\
| maxBackEndBandwidth | Integer | Maximum amount of CPU capacity allotted for HCP processes. |\
| maxFrontEndBandwidth | Integer | The maximum amount of CPU capacity allotted for HCP processes. |\
| maxHttpConnections | Integer | Maximum number of HTTP connections. |\
| maxHttpsConnections | Integer | Maximum number of HTTPS connections. |\
| maxManagementPortBandwidth | Integer | Maximum amount of CPU capacity allotted for HCP processes. This is returned only if you have configured the management port network on the node. |\
| nodeNumber | Integer | Number of the node. |\
| openHttpConnections | Integer | Number of open HTTP connections. |\
| openHttpsConnections | Integer | Number of open HTTPS connections. |\
| requestTime | Integer | Exact time that the request was issued in milliseconds. |\
| swapOut | Float | Average number of pages swapped out of memory per second. |\
| totalBytes | Long | Total size of an HCP volume. |\
| totalInodes | Long | Maximum inodes limit of a HCP volume. |\
| transferSpeed | Float | Transfer speed for the logical volumes specified as the number of input/output requests per second. |\
\
## Example\
\
Here’s an XML example of the `nodeStatistics` data type:\
\
```\
<nodeStatistics>\
     <requestTime>1528292517330</requestTime>\
     <nodes>\
          <node>\
               <nodeNumber>17</nodeNumber>\
               <frontendIpAddresses>\
                    <ipAddress>172.20.35.17</ipAddress>\
                    <ipAddress>2001:623:0:0:222:11ff:fec7:6584</ipAddress>\
               </frontendIpAddresses>\
               <backendIpAddress>172.35.14.17</backendIpAddress>\
               <managementIpAddresses>\
                    <ipAddress>172.20.45.17</ipAddress>\
                    <ipAddress>2001:623:0:0:222:11ff:fec7:6585</ipAddress>\
               </managementIpAddresses>\
               <openHttpConnections>0</openHttpConnections>\
               <openHttpsConnections>0</openHttpsConnections>\
               <maxHttpConnections>255</maxHttpConnections>\
               <maxHttpsConnections>254</maxHttpsConnections>\
               <cpuUser>0.16</cpuUser>\
               <cpuSystem>0.08</cpuSystem>\
               <cpuMax>24</cpuMax>\
               <ioWait>0.02</ioWait>\
               <swapOut>0.0</swapOut>\
               <maxFrontEndBandwidth>1024000</maxFrontEndBandwidth>\
               <frontEndBytesRead>0.3</frontEndBytesRead>\
               <frontEndBytesWritten>0.2</frontEndBytesWritten>\
               <maxBackEndBandwidth>1024000</maxBackEndBandwidth>\
               <backEndBytesRead>7.22</backEndBytesRead>\
               <backEndBytesWritten>3.87</backEndBytesWritten>\
               <maxManagementPortBandwidth>1024000</maxManagementPortBandwidth>\
               <managementBytesRead>.75</managementBytesRead>\
               <managementBytesWritten>.7</managementBytesWritten>\
               <collectionTimestamp>1528292472000</collectionTimestamp>\
               <volumes>\
                    <volume>\
                         <id>example090</id>\
                         <blocksRead>0.0</blocksRead>\
                         <blocksWritten>5.17</blocksWritten>\
                         <diskUtilization>0.2</diskUtilization>\
                         <transferSpeed>0.35</transferSpeed>\
                         <totalBytes>10223583</totalBytes>\
                         <freeBytes>37940648</freeBytes>\
                         <totalInodes>10223616</totalInodes>\
                         <freeInodes>38073632</freeInodes>\
                    </volume>\
                    <volume>\
                         <id>example091</id>\
                         <blocksRead>0.0</blocksRead>\
                         <blocksWritten>200.33</blocksWritten>\
                         <diskUtilization>0.13</diskUtilization>\
                         <transferSpeed>2.45</transferSpeed>\
                         <totalBytes>10217599</totalBytes>\
                         <freeBytes>37887380</freeBytes>\
                         <totalInodes>10443212</totalInodes>\
                         <freeInodes>40083820</freeInodes>\
                    </volume>\
               </volumes>\
          </node>\
          <node>\
              . . . .\
          </node>\
     </nodes>\
</nodeStatistics>\
```\
\
### protocols\
\
The `protocols` data type describes the `protocols` resource for default namespaces.\
\
Note: For HCPnamespaces, the protocols resource is superseded by the .../protocols/http resource, which has a data type of `httpProtocol`.\
\
\
## Properties\
\
The table below describes the properties included in the `protocols` data type.\
\
| Property | Data type | Description |\
| --- | --- | --- |\
| httpEnabled | Boolean | Specifies whether the HTTP protocol is enabled for the namespace. Valid values are:<br>true<br> HTTP is enabled.false<br> HTTP is disabled.<br>The default is false. |\
| httpsEnabled | Boolean | Specifies whether the HTTPS protocol is enabled for the namespace. Valid values are:<br>true<br> HTTPS is enabled.false<br> HTTPS is disabled.<br>The default is true. |\
| ipSettings | ipSettings | Specifies which IP addresses can and cannot access the namespace through the HTTP and HTTPS protocols. |\
\
## Example\
\
Here’s an XML example of the `protocols` data type:\
\
```\
<protocols>\
    <httpEnabled>false</httpEnabled>\
    <httpsEnabled>true</httpsEnabled>\
    <ipSettings>\
     <allowAddresses>\
         <ipAddress>192.168.140.10</ipAddress>\
             <ipAddress>192.168.140.14</ipAddress>\
             <ipAddress>192.168.140.15</ipAddress>\
            <ipAddress>192.168.149.0/24</ipAddress>\
         </allowAddresses>\
        <allowIfInBothLists>false</allowIfInBothLists>\
         <denyAddresses>\
             <ipAddress>192.168.149.5</ipAddress>\
         </denyAddresses>\
     </ipSettings>\
</protocols>\
```\
\
### recipient\
\
The `recipient` data type describes the recipient property of the `recipients` data type.\
\
## Properties\
\
The table below describes the properties included in the `recipient` data type.\
\
| Property | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| address | String | Specifies a comma-separated list of well-formed email addresses for notification about messages added to the tenant log. | This property is required on a POST request. |\
| importance | String | Specifies whether HCP should send email only about important log messages to the specified email addresses or send email about all log messages. Valid values are:<br>ALL<br> Send email about all log messages.MAJOR<br> Send email only about important log messages.<br>The default is MAJOR.<br>These values are not case sensitive. |  |\
| severity | String | Specifies the severity level of the log messages about which to send email. Valid values are:<br>NOTICE<br> Send email about log messages with a severity level of notice, warning, or error.WARNING<br> Send email about log messages with a severity level of warning or error.ERROR<br> Send email only about log messages with a severity level of error.<br>The default is ERROR.<br>These values are not case sensitive. |  |\
| type | String | Specifies the types of log messages about which to send email. Valid values are comma-separated lists of one or more of:<br>GENERAL<br> Send email about log messages that do not have a type of security or compliance.SECURITY<br> Send email about log messages with a type of security.COMPLIANCE<br> Send email about log messages with a type of compliance.<br>The default is GENERAL.<br>These values are not case sensitive. |  |\
\
## Example\
\
Here’s an XML example of the recipient data type:\
\
```\
<recipient>\
    <address>lgreen@example.com, sgold@example.com</address>\
    <importance>MAJOR</importance>\
    <severity>ERROR</severity>\
    <type>GENERAL,SECURITY</type>\
</recipient>\
```\
\
### recipients\
\
The `recipients` data type describes the `recipients` property of the `emailNotification` data type.\
\
## Properties\
\
The table below describes the property included in the `recipients` data type.\
\
| Property | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| recipient | recipient | Specifies:<br>- One or more email addresses to which HCP sends email about log messages<br>- The types of log messages about which HCP sends email to those addresses | Include one instance of this property (up to 25) for each set of email addresses you want in the recipients list for email notification.<br>The set of recipients specified in the request body replaces the set of recipients currently configured for email notification.<br>Recipients are added to the Bcc list for each email.<br>Because each instance of the recipient property can specify multiple email addresses, you can specify a total of more than 25 addresses across all instances of this property. However, if you specify more than 25, HCP sends each email only to an arbitrary 25 of them. |\
\
## Example\
\
Here’s an XML example of the recipients data type property:\
\
```\
<recipients>\
    <recipient>\
         <address>lgreen@example.com, sgold@example.com</address>\
         <importance>MAJOR</importance>\
         <severity>ERROR</severity>\
         <type>GENERAL,SECURITY</type>\
    </recipient>\
    <recipient>\
         <address>mwhite@example.com</address>\
         <importance>ALL</importance>\
         <severity>WARNING</severity>\
         <type>COMPLIANCE</type>\
    </recipient>\
</recipients>\
```\
\
### remote (data type for replication link failoverSettings remote property)\
\
The `local` data type in this section describes the `remote` property of the `failoverSettings` data type.\
\
## Replication link failoverSettings remote data type properties\
\
The following table describes the properties included in the `remote` data type that describes the `remote` property of the `failoverSettings` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| autoCompleteRecovery | Boolean | For an active/passive link, specifies whether the complete recovery phase starts automatically while data is being recovered from the remote system for the replication link to the local system. Valid values are:<br>true<br> The complete recovery phase starts automatically.false<br> The complete recovery phase does not start automatically.<br>The default is false. | This property is optional on a PUT request to create an active/passive link. It is not valid on a PUT or POST request for an active/active link and is not returned by any GET request for an active/active link. |\
| autoCompleteRecovery<br>Minutes | Integer | For an active/passive link, specifies the number of minutes the up-to-date-as-of time for the replication link must be less than before HCP automatically starts the complete recovery phase while data is being recovered from the remote system for the link to the local system. Valid values are integers in the range one through 9,999. The default is 120.<br>The up-to-date-as-of time is the difference between:<br>- The date and time before which configuration changes and changes to namespace content are guaranteed to have been sent to the local system<br>- The current date and time | This property is optional on a PUT request to create an active/passive link. It is not valid on a PUT or POST request for an active/active link and is not returned by any GET request for an active/active link. |\
| autoFailover | Boolean | Specifies whether the replication link automatically fails over to the remote system for the link after a loss of connectivity to the local system. Valid values are:<br>true<br> The link automatically fails over.false<br> The link does not fail over automatically.<br>The default is false. | This property is optional on a PUT request to create a replication link. |\
| autoFailoverMinutes | Integer | Specifies the number of minutes HCP waits before automatically failing over the replication link to the remote system for the link after a loss of connectivity to the local. Valid values are integers in the range seven through 9,999. The default is 120. | This property is optional on a PUT request to create a replication link. |\
\
## Example\
\
Here’s an XML example of the `remote` data type that describes the `remote` property of the `failoverSettings` data type; the properties shown are those that are returned by a GET request sent to the primary system for an active/passive link:\
\
```\
<remote>\
    <autoFailover>true</autoFailover>\
    <autoFailoverMinutes>120</autoFailoverMinutes>\
    <autoCompleteRecovery>false</autoCompleteRecovery>\
    <autoCompleteRecoveryMinutes>60</autoCompleteRecoveryMinutes>\
</remote>\
```\
\
### remote (data type for replication link schedule remote property)\
\
The `remote` data type in this section describes the `remote` property of the `schedule` data type that describes the `schedule` resource for replication links.\
\
## Replication link schedule local data type properties\
\
The following table describes the properties included in the `local` data type that describes the `local` property of the `schedule` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| scheduleOverride | String | Specifies an override for the local schedule for the replication link. Valid values are:<br>LOW<br> The performance level is low for the entire week.MEDIUM<br> The performance level is medium for the entire week.HIGH<br> The performance level is high for the entire week.CUSTOM<br> The performance level is the custom setting for the entire week.NONE<br> The schedule for the link is not overridden.<br>These values are not case-sensitive.<br>To remove an existing override, specify NONE as the value for this property. If you don’t explicitly remove an existing override when changing the local schedule, the override remains in effect. |  |\
| transition | transition | Specifies a scheduled change of performance level for the replication link on the local system. |  |\
\
## Example\
\
Here’s an XML example of the `local` data type that describes that describes the `local` property of the `schedule` data type:\
\
```\
<remote>\
    <scheduleOverride>NONE</scheduleOverride>\
    <transition>\
        <time>Sun:00</time>\
        <performanceLevel>HIGH</performanceLevel>\
    </transition>\
    <transition>\
        <time>Mon:00</time>\
        <performanceLevel>MEDIUM</performanceLevel>\
    </transition>\
</remote>\
```\
\
### replicationCollisionSettings\
\
The `replicationCollisionSettings` data type describes the `replicationCollisionSettings` resource for namespaces.\
\
## Properties\
\
The table below describes the properties included in the `replicationCollisionSettings` data type.\
\
| Property | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| action | String | Specifies how HCP should handle objects flagged as replication collisions. Valid values are:<br>MOVE<br> <br> Move the object to the .lost+found directory in the same namespace.<br> RENAME<br> Rename the object in place.<br> When renaming an object, HCP changes the object name to `object-name.collision`.<br> <br>These values are not case sensitive.<br>The default is MOVE. |  |\
| deleteDays | Integer | Specifies the number of days objects flagged as replication collisions must remain in the namespace before they are automatically deleted. Valid values are integers in the range zero through 36,500 (that is, 100 years). A value of zero means delete immediately. | This property is required on a POST request when the value of the `deleteEnabled` property is true. It is invalid on a POST request when the value of the deleteEnabled property is false. |\
| deleteEnabled | Boolean | Specifies whether HCP should automatically delete objects flagged as replication collisions. Valid values are:<br>true<br> <br> Automatically delete objects flagged as replication collisions after the number of days specified by the `deleteDays` property.<br> false<br> Do not automatically delete objects flagged as replication collisions. |  |\
\
## Example\
\
Here’s an XML example of the `replicationCollisionSettings` data type property:\
\
```\
<replicationCollisionSettings>\
    <action>MOVE</action>\
    <deleteDays>10</deleteDays>\
    <deleteEnabled>true</deleteEnabled>\
</replicationCollisionSettings>\
```\
\
### replicationLink\
\
The `replicationLink` property describes the `replicationLinks` property of the `linkCandidates` resource for erasure coding and the `replicationLinks` property of the `ecTopology` data type.\
\
## replicationLink data type properties\
\
The following table describes the properties included in the `replicationLink` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| hcpSystems | List | Lists the two HCP systems included in the replication link. To specify a system, use the fully qualified name of the domain associated with the \[hcp\_system\] network on that system. | This property is optional on a PUT request to create an erasure coding topology.<br>If a PUT request to create an erasure coding topology does not include the uuid property for a replication link and the name specified for the link is the same as the name of one or more other links, HCP may not know which link you want to include in the topology. To ensure that the correct link is used, the request should include the hcpSystems property for the link you want.<br>In XML, the element that identifies each system is name. In JSON, the name in the name/value pair that lists the systems is name. |\
| name | String | Specifies the name of the replication link. | This property is required on a PUT request to create an erasure coding topology if the request does not include the uuid property for the replication link. Otherwise, the name property is optional. |\
| pausedTenantsCount | Integer | Specifies the number of tenants for which replication on the link is currently paused. | This property is not valid on a PUT request to create an erasure coding topology. It is returned only by a verbose GET request. |\
| state | String | Indicates the general health of the replication link. Possible values are:<br>HEALTHY<br> <br> The specific status of the link is one of these:<br> <br>- oSynchronizing data<br>- oScheduled data<br>NOT\_REPLICATING<br> <br> The specific status of the link is one of these:<br> <br>- oSuspended by user<br>- oRemote storage full, link suspended<br>- oLocal storage full, link suspended<br>- oFailed over<br>UNHEALTHY<br> <br> The specific status of the link is one of these:<br> <br>- oHigh error rate<br>- oStalled link<br>- oUnrecognized link<br>- oBroken link<br>UNKNOWN<br> HCP cannot determine the specific status of the link. | This property is not valid on a PUT request to create an erasure coding topology. It is returned only by a verbose GET request. |\
| uuid | String | Specifies the unique ID for the replication link. | This property is not valid on a PUT request to create an erasure coding topology. It is returned only by a verbose GET request. |\
\
## Example\
\
Here's an XML example of the `replicationLink` data type; the properties shown are those that are returned in response to a verbose GET request:\
\
```\
<replicationLink>\
    <hcpSystems>\
        <name>hcp-ca.example.com</name>\
        <name>hcp-eu.example.com</name>\
    </hcpSystems>\
    <name>eu-ca</name>\
    <pausedTenantsCount>0</pausedTenantsCount>\
    <state>HEALTHY</state>\
    <uuid>7ae4101c-6e29-426e-ae71-9a7a529f019d</uuid>\
</replicationLink>\
```\
\
### replicationLinks\
\
The `replicationLinks` data type describes the `linkCandidates` resource for erasure coding and the `replicationLinks` property of the `ecTopology` data type.\
\
## replicationLinks data type property\
\
The following table describes the property included in the `replicationLinks` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| replicationLink | replication Link | Specifies a replication link. | In a PUT request to create an erasure coding topology, include one instance of this property for each link you want to include in the topology. |\
\
## Example\
\
Here's an XML example of the `replicationLinks` data type; the properties shown are those that are returned in response to a verbose GET request:\
\
```\
<replicationLinks>\
    <replicationLink>\
        <hcpSystems>\
            <name>hcp-ca.example.com</name>\
            <name>hcp-eu.example.com</name>\
        </hcpSystems>\
        <name>eu-ca</name>\
        <pausedTenantsCount>0</pausedTenantsCount>\
        <state>HEALTHY</state>\
        <uuid>7ae4101c-6e29-426e-ae71-9a7a529f019d</uuid>\
    </replicationLink>\
    <replicationLink>\
        <hcpSystems>\
            <name>hcp-ca.example.com</name>\
            <name>hcp-us.example.com</name>\
        </hcpSystems>\
        <name>us-ca</name>\
        <pausedTenantsCount>4</pausedTenantsCount>\
        <state>HEALTHY</state>\
        <uuid>cdb7edcd-feb6-4476-8d8d-bd053e3bc2ee</uuid>\
    </replicationLink>\
    <replicationLink>\
        <hcpSystems>\
            <name>hcp-eu.example.com</name>\
            <name>hcp-us.example.com.com</name>\
        </hcpSystems>\
        <name>us-eu</name>\
        <pausedTenantsCount>0</pausedTenantsCount>\
        <state>HEALTHY</state>\
        <uuid>32871da5-2355-458a-90f5-1717aa684d6f</uuid>\
    </replicationLink>\
</replicationLinks>\
```\
\
### replicationService\
\
The `replicationService` data type describes the `replication` resource.\
\
## replicationService data type properties\
\
The following table describes the properties included in the `replicationService` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| allowTenantsToMonitor<br>Namespaces | Boolean | Specifies whether the Tenant Management Console for HCP tenants displays the status of replication of the tenant and its namespaces. Valid values are:<br>true<br> <br> The Tenant Management Console displays replication status information for all HCP tenants.<br> false<br> <br> The Tenant Management Console hide replication status information for all HCP tenants.<br> <br>The default is false. |  |\
| connectivityTimeout<br>Seconds | Integer | Specifies how long the HCP system should wait before reporting a replication link connectivity failure. Valid values are integers greater than or equal to zero. |  |\
| enableDNSFailover | Boolean | Specifies whether DNS failover is enabled for the HCP system. Valid values are:<br>true<br> DNS failover is enabled for the system.false<br> DNS failover is disabled for the system.<br>The default is false. |  |\
| enableDomainAnd<br>Certificate<br>Synchronization | Boolean | Specifies whether HCP periodically sends its domains and SSL server certificates to each other HCP system with which it participates as a sending system in a replication link. Valid values are:<br>true<br> HCP periodically sends its domains and SSL server certificates to each other system with which it participates as a sending system in a replication link.<br> false<br> HCP does not send its domains and SSL server certificates to other systems.<br> <br>The default is false. | This data type has been deprecated and should not be used. |\
| network | String | Specifies the replication network for the HCP system. Valid values are any network defined in the HCP system except \[hcp\_backend\]. The default is \[hcp\_system\].<br>Network names are not case-sensitive. |  |\
| status | String | Specifies whether all activity on all replication links in which the HCP system participates is currently stopped. Possible values are:<br>ENABLED<br> Activity on each replication link in which the system participates is occurring according the individual link status.SHUTDOWN<br> All activity on all replication links in which the system participates is currently stopped. | This property is not valid on a POST request. It is returned only by a verbose GET request. |\
| verification | String | Specifies whether replication verification is enabled for the HCP system. Possible values are:<br>ON<br> Replication verification is set to continuously run.ONCE<br> Replication verification is set to run only on.OFF<br> Replication verification is disabled. |  |\
\
## Example\
\
Here’s an XML example of the `replicationService` data type:\
\
```\
<replicationService>\
    <allowTenantsToMonitorNamespaces>false</allowTenantsToMonitorNamespaces>\
    <enableDNSFailover>true</enableDNSFailover>\
    <network>[hcp_system]</network>\
    <connectivityTimeoutSeconds>30</connectivityTimeoutSeconds>\
    <status>ENABLED</status>\
    <verification>ONCE</verification>\
</replicationService>\
```\
\
## Query parameters for Replication service actions\
\
To shut down all replication links in which the HCP system participates, you use this query parameter:\
\
```\
shutDownAllLinks=reason\
```\
\
reason is a text string that specifies the reason why you’re shutting down all links. This string can be up to 1,024 characters long and can contain any valid UTF-8 characters, including white space. The string you specify must be percent encoded.\
\
To reestablish all replication links in which the HCP system participates after they have been shut down, you use this query parameter:\
\
```\
reestablishAllLinks\
```\
\
You use the shutDownAllLinks and reestablishAllLinks query parameters with a POST request against the replication resource. You cannot include a request body with this request.\
\
Here’s a sample POST request that shuts down all replication links:\
\
```\
curl -k -iX POST\
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"\
    "https://admin.hcp-ma.example.com:9090/mapi/services/replication\
       ?shutDownAllLinks=More%20bandwidth%20for%20app%20XYZ"\
```\
\
### retentionClass\
\
The `retentionClass` data type describes the `retentionClasses` resource.\
\
## Properties\
\
The table below describes the properties included in the `retentionClass` data type.\
\
| Property | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| allowDisposition | Boolean | Specifies whether HCP automatically deletes expired objects in the retention class. Valid values are:<br>true<br> Automatically delete expired objects.false<br> Do not automatically delete expired objects. | This property is required on a PUT request when the value of the retention class is an offset. It is ignored if the value is not an offset.<br>This property is required on a POST request when the retention class value is being changed to an offset from another type of value. |\
| description | String | Specifies a description of the retention class. This description is optional. The default is no description.<br>To remove a description from an existing retention class, specify the description property with no value. |  |\
| name | String | Specifies the name of the retention class. Retention class names must be from one through 64 characters long, can contain only alphanumeric characters, hyphens (-), and underscores (\_), and are not case sensitive.<br>The retention class name must be unique for the namespace. Different namespaces can have retention classes with the same name. | This property is required on a PUT request. It is not valid on a POST request and is returned only by a verbose GET request. |\
| value | String | Specifies the retention class value. Valid values are special values and offsets. | This property is required on a PUT request. |\
\
## Example\
\
Here’s an XML example of the `retentionClass` data type:\
\
```\
<retentionClass>\
    <description>Implements Finance department standard #42 - keep for 10\
         years.</description>\
    <value>A+10y</value>\
    <allowDisposition>true</allowDisposition>\
    <name>FN-Std-42</name>\
</retentionClass>\
```\
\
### schedule\
\
The `schedule` data type describes the `schedule` resource for replication links.\
\
## schedule data type properties\
\
The following table describes the properties included in the `schedule` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| local | local | Specifies the schedule for the replication link on the local system. | This property is not valid on a POST request for an inbound active/passive link. |\
| remote | remote | Specifies the schedule for the replication link on the remote system. | This property is not valid on a POST request for an outbound active/passive link. |\
\
## Example\
\
Here’s an XML example of the `schedule` data type:\
\
```\
<schedule>\
    <local>\
         <scheduleOverride>NONE</scheduleOverride>\
         <transition>\
             <time>Sun:00</time>\
             <performanceLevel>HIGH</performanceLevel>\
         </transition>\
         <transition>\
             <time>Sun:06</time>\
             <performanceLevel>MEDIUM</performanceLevel>\
         </transition>\
         <transition>\
             <time>Sat:00</time>\
             <performanceLevel>HIGH</performanceLevel>\
         </transition>\
         <transition>\
             <time>Sat:06</time>\
             <performanceLevel>MEDIUM</performanceLevel>\
         </transition>\
    </local>\
    <remote>\
         <scheduleOverride>NONE</scheduleOverride>\
         <transition>\
             <time>Sun:00</time>\
             <performanceLevel>HIGH</performanceLevel>\
         </transition>\
         <transition>\
             <time>Mon:00</time>\
             <performanceLevel>MEDIUM</performanceLevel>\
         </transition>\
    </remote>\
</schedule>\
```\
\
### searchSecurity\
\
The `searchSecurity` data type describes the `searchSecurity` resource for tenants.\
\
## Properties\
\
The table below describes the property included in the `searchSecurity` data type.\
\
| Property | Data type | Description |\
| --- | --- | --- |\
| ipSettings | ipSettings | Specifies which IP addresses can and cannot access the Search Console for the tenant. |\
\
## Example\
\
Here’s an XML example of the `searchSecurity` data type:\
\
```\
<searchSecurity>\
    <ipSettings>\
         <allowAddresses>\
             <ipAddress>192.168.140.36</ipAddress>\
             <ipAddress>192.168.140.109</ipAddress>\
         </allowAddresses>\
         <allowIfInBothLists>false</allowIfInBothLists>\
         <denyAddresses/>\
    </ipSettings>\
</searchSecurity>\
```\
\
### service\
\
The `service` data type retrieves information about the services used by an HCP system.\
\
## service data type properties\
\
The following table describes the properties included in the `service` data type.\
\
| Property name | Data type | Description |\
| --- | --- | --- |\
| endTime | Integer | Time that the service finished running. A value of -1 indicates that the service has not finished running. |\
| formattedStartTime | String | Date and time that the service started running since the last run. This value is formatted for easy reading.<br>This value is returned only if the service has run at least once. |\
| formattedEndTime | String | Date and time that the service finished running since the last run. This value is formatted for easy reading.<br>This value is returned only if the service has run at least once. |\
| name | String | Name of the service. |\
| objectsExamined | Integer | Number of objects that the service examined. |\
| objectsExaminedPerSecond | Double | Number of objects per second that the service examined. |\
| objectsServiced | Integer | Number of objects that were serviced by the service. |\
| objectsServicedPerSecond | Double | Number of objects per second that were serviced by the service. |\
| objectsUnableToService | Integer | Number of objects that the service could not service. |\
| objectsUnableToServicePerSecond | Double | Number of objects per second that the service could not service. |\
| performanceLevel | Integer | Performance level of the service. This is returned only if the state of the service is RUNNING or STOPPING. |\
| startTime | Integer | Time that the service started running. A value of -1 indicates that the service has not started running yet. |\
| state | String | Current state of the service. |\
\
## Example\
\
For an XML example of the `service` data type see [serviceStatistics](https://docs.hitachivantara.com/internal/api/webapp/print/62f927be-7881-44d6-92bb-5b476aa3789e#GUID-AFDE6478-F5F1-4A4B-AD2B-8279B41E79AE).\
\
### services\
\
The `services` data type for the services property of the nodeStatistics and serviceStatistics resources retrieves information about the services that run on an HCP system:\
\
\
- The nodeStatistics resource returns information about services for each individual node.\
- The serviceStatistics resource returns aggregated information about services across the HCP system.\
\
## services data type properties\
\
The following table describes the property included in the `services` data type.\
\
| Property name | Data type | Description |\
| --- | --- | --- |\
| service | service | A service that runs on an HCP system. |\
\
## Example\
\
For an XML example of the `services` data type, see [nodeStatistics](https://docs.hitachivantara.com/internal/api/webapp/print/62f927be-7881-44d6-92bb-5b476aa3789e#GUID-BB8F9EF6-9643-4935-8D8A-EA33BD6C21AC) or [serviceStatistics](https://docs.hitachivantara.com/internal/api/webapp/print/62f927be-7881-44d6-92bb-5b476aa3789e#GUID-AFDE6478-F5F1-4A4B-AD2B-8279B41E79AE), as applicable.\
\
### serviceStatistics\
\
The `serviceStatistics` data type retrieves information about the statistics of services that run on your HCP system. The output starts with a cumulative cluster-wide statistics for each service followed by a list of services on a per-node basis.\
\
## serviceStatistics data type properties\
\
The following table describes the properties included in the `serviceStatistics` data type.\
\
| Property name | Data type | Description |\
| --- | --- | --- |\
| nodes |  |  |\
| requestTime | Integer | Specifies the time of the request for the service Statistics resource, in milliseconds, since January 1, 1970, at 00:00:00 UTC. |\
| services | services | Services that run on your HCP system. |\
\
## Example\
\
Here is an XML example of the `serviceStatistics` data type:\
\
```\
<serviceStatistics>\
    <requestTime>1610549692631</requestTime>\
    <services>\
        <service>\
            <name>StorageTieringService</name>\
            <state>READY</state>\
            <startTime>1603123200</startTime>\
            <formattedStartTime>10/19/2020 12:00:00 EDT</formattedStartTime>\
            <endTime>-1</endTime>\
            <formattedEndTime>11/29/2020 18:42:09 EST</formattedEndTime>\
            <objectsExamined>107026773</objectsExamined>\
            <objectsExaminedPerSecond>4847.1900000000005</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>RetirementPolicy</name>\
            <state>DISABLED</state>\
            <startTime>-1</startTime>\
            <endTime>-1</endTime>\
            <objectsExamined>0</objectsExamined>\
            <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>MigrationVerificationPolicy</name>\
            <state>DISABLED</state>\
            <startTime>-1</startTime>\
            <endTime>-1</endTime>\
            <objectsExamined>0</objectsExamined>\
            <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>ContentVerification</name>\
            <state>RUNNING</state>\
            <startTime>1610514000</startTime>\
            <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
            <endTime>-1</endTime>\
            <performanceLevel>HIGH</performanceLevel>\
            <objectsExamined>15283838</objectsExamined>\
            <objectsExaminedPerSecond>428.23999999999995</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>55</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>GarbageCollection</name>\
            <state>RUNNING</state>\
            <startTime>1610514000</startTime>\
            <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
            <endTime>-1</endTime>\
            <performanceLevel>HIGH</performanceLevel>\
            <objectsExamined>8664076</objectsExamined>\
            <objectsExaminedPerSecond>242.75999999999996</objectsExaminedPerSecond>\
            <objectsServiced>2729282</objectsServiced>\
            <objectsServicedPerSecond>76.47</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>MigrationPolicy</name>\
            <state>DISABLED</state>\
            <startTime>-1</startTime>\
            <endTime>-1</endTime>\
            <objectsExamined>0</objectsExamined>\
            <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>CapacityBalancePolicy</name>\
            <state>READY</state>\
            <startTime>-1</startTime>\
            <endTime>-1</endTime>\
            <objectsExamined>0</objectsExamined>\
            <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>SNodeCapacityBalanceService</name>\
            <state>READY</state>\
            <startTime>1603123200</startTime>\
            <formattedStartTime>10/19/2020 12:00:00 EDT</formattedStartTime>\
            <endTime>-1</endTime>\
            <formattedEndTime>10/19/2020 12:00:00 EDT</formattedEndTime>\
            <objectsExamined>10</objectsExamined>\
            <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>ErasureCodingService</name>\
            <state>READY</state>\
            <startTime>1610470800</startTime>\
            <formattedStartTime>1/12/2021 12:00:00 EST</formattedStartTime>\
            <endTime>1610514019</endTime>\
            <formattedEndTime>1/13/2021 0:00:19 EST</formattedEndTime>\
            <objectsExamined>303769743</objectsExamined>\
            <objectsExaminedPerSecond>7030.9800000000005</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>DispositionPolicy</name>\
            <state>DISABLED</state>\
            <startTime>-1</startTime>\
            <endTime>-1</endTime>\
            <objectsExamined>0</objectsExamined>\
            <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>ScavengingPolicy</name>\
            <state>READY</state>\
            <startTime>1610254800</startTime>\
            <formattedStartTime>1/10/2021 0:00:00 EST</formattedStartTime>\
            <endTime>1610305205</endTime>\
            <formattedEndTime>1/10/2021 14:00:05 EST</formattedEndTime>\
            <objectsExamined>20923214</objectsExamined>\
            <objectsExaminedPerSecond>415.11</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>SNodeCompressionEncryptionValidationService</name>\
            <state>DISABLED</state>\
            <startTime>-1</startTime>\
            <endTime>-1</endTime>\
            <objectsExamined>0</objectsExamined>\
            <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>ProtectionPolicy</name>\
            <state>READY</state>\
            <startTime>1610254800</startTime>\
            <formattedStartTime>1/10/2021 0:00:00 EST</formattedStartTime>\
            <endTime>1610305275</endTime>\
            <formattedEndTime>1/10/2021 14:01:15 EST</formattedEndTime>\
            <objectsExamined>37350934</objectsExamined>\
            <objectsExaminedPerSecond>740.68</objectsExaminedPerSecond>\
            <objectsServiced>0</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>DuplicateElimination</name>\
            <state>RUNNING</state>\
            <startTime>1610514000</startTime>\
            <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
            <endTime>-1</endTime>\
            <performanceLevel>LOW</performanceLevel>\
            <objectsExamined>44671117</objectsExamined>\
            <objectsExaminedPerSecond>1251.6699999999998</objectsExaminedPerSecond>\
            <objectsServiced>11</objectsServiced>\
            <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
        <service>\
            <name>CompressionPolicy</name>\
            <state>READY</state>\
            <startTime>1610463600</startTime>\
            <formattedStartTime>1/12/2021 10:00:00 EST</formattedStartTime>\
            <endTime>1610478000</endTime>\
            <formattedEndTime>1/12/2021 14:00:00 EST</formattedEndTime>\
            <objectsExamined>530581</objectsExamined>\
            <objectsExaminedPerSecond>36.849999999999994</objectsExaminedPerSecond>\
            <objectsServiced>510431</objectsServiced>\
            <objectsServicedPerSecond>35.45</objectsServicedPerSecond>\
            <objectsUnableToService>0</objectsUnableToService>\
            <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
        </service>\
    </services>\
    <nodes>\
        <node>\
            <nodeNumber>24</nodeNumber>\
            <frontendIpAddresses>\
                <ipAddress>172.18.120.24</ipAddress>\
            </frontendIpAddresses>\
            <managementIpAddresses>\
                <ipAddress>172.18.200.248</ipAddress>\
            </managementIpAddresses>\
            <ipAddress>172.120.24.24</ipAddress>\
            <services>\
                <service>\
                    <name>StorageTieringService</name>\
                    <state>READY</state>\
                    <startTime>1603123200</startTime>\
                    <formattedStartTime>10/19/2020 12:00:00 EDT</formattedStartTime>\
                    <endTime>1603139389</endTime>\
                    <formattedEndTime>10/19/2020 16:29:49 EDT</formattedEndTime>\
                    <objectsExamined>47087238</objectsExamined>\
                    <objectsExaminedPerSecond>2908.59</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>RetirementPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>MigrationVerificationPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ContentVerification</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514000</startTime>\
                    <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>HIGH</performanceLevel>\
                    <objectsExamined>3679166</objectsExamined>\
                    <objectsExaminedPerSecond>103.08</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>55</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>GarbageCollection</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514000</startTime>\
                    <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>HIGH</performanceLevel>\
                    <objectsExamined>2607388</objectsExamined>\
                    <objectsExaminedPerSecond>73.05</objectsExaminedPerSecond>\
                    <objectsServiced>623221</objectsServiced>\
                    <objectsServicedPerSecond>17.46</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>MigrationPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>CapacityBalancePolicy</name>\
                    <state>READY</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>SNodeCapacityBalanceService</name>\
                    <state>READY</state>\
                    <startTime>1603123200</startTime>\
                    <formattedStartTime>10/19/2020 12:00:00 EDT</formattedStartTime>\
                    <endTime>1603123200</endTime>\
                    <formattedEndTime>10/19/2020 12:00:00 EDT</formattedEndTime>\
                    <objectsExamined>5</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ErasureCodingService</name>\
                    <state>READY</state>\
                    <startTime>1610470800</startTime>\
                    <formattedStartTime>1/12/2021 12:00:00 EST</formattedStartTime>\
                    <endTime>1610514000</endTime>\
                    <formattedEndTime>1/13/2021 0:00:00 EST</formattedEndTime>\
                    <objectsExamined>50616546</objectsExamined>\
                    <objectsExaminedPerSecond>1171.68</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>DispositionPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ScavengingPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610254800</startTime>\
                    <formattedStartTime>1/10/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>1610305205</endTime>\
                    <formattedEndTime>1/10/2021 14:00:05 EST</formattedEndTime>\
                    <objectsExamined>6650138</objectsExamined>\
                    <objectsExaminedPerSecond>131.93</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>SNodeCompressionEncryptionValidationService</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ProtectionPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610254800</startTime>\
                    <formattedStartTime>1/10/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>1610305275</endTime>\
                    <formattedEndTime>1/10/2021 14:01:15 EST</formattedEndTime>\
                    <objectsExamined>9127341</objectsExamined>\
                    <objectsExaminedPerSecond>180.83</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>DuplicateElimination</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514000</startTime>\
                    <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>LOW</performanceLevel>\
                    <objectsExamined>11169644</objectsExamined>\
                    <objectsExaminedPerSecond>312.95</objectsExaminedPerSecond>\
                    <objectsServiced>3</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>CompressionPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610463600</startTime>\
                    <formattedStartTime>1/12/2021 10:00:00 EST</formattedStartTime>\
                    <endTime>1610478000</endTime>\
                    <formattedEndTime>1/12/2021 14:00:00 EST</formattedEndTime>\
                    <objectsExamined>137537</objectsExamined>\
                    <objectsExaminedPerSecond>9.55</objectsExaminedPerSecond>\
                    <objectsServiced>131946</objectsServiced>\
                    <objectsServicedPerSecond>9.16</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
            </services>\
        </node>\
        <node>\
            <nodeNumber>25</nodeNumber>\
            <frontendIpAddresses>\
                <ipAddress>172.18.120.25</ipAddress>\
            </frontendIpAddresses>\
            <managementIpAddresses>\
                <ipAddress>172.18.200.249</ipAddress>\
            </managementIpAddresses>\
            <ipAddress>172.120.24.25</ipAddress>\
            <services>\
                <service>\
                    <name>StorageTieringService</name>\
                    <state>READY</state>\
                    <startTime>1606662410</startTime>\
                    <formattedStartTime>11/29/2020 10:06:50 EST</formattedStartTime>\
                    <endTime>1606693329</endTime>\
                    <formattedEndTime>11/29/2020 18:42:09 EST</formattedEndTime>\
                    <objectsExamined>59939535</objectsExamined>\
                    <objectsExaminedPerSecond>1938.6</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>RetirementPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>MigrationVerificationPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ContentVerification</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514000</startTime>\
                    <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>HIGH</performanceLevel>\
                    <objectsExamined>3892984</objectsExamined>\
                    <objectsExaminedPerSecond>109.07</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>GarbageCollection</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514000</startTime>\
                    <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>HIGH</performanceLevel>\
                    <objectsExamined>2010985</objectsExamined>\
                    <objectsExaminedPerSecond>56.34</objectsExaminedPerSecond>\
                    <objectsServiced>573076</objectsServiced>\
                    <objectsServicedPerSecond>16.06</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>MigrationPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>CapacityBalancePolicy</name>\
                    <state>READY</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>SNodeCapacityBalanceService</name>\
                    <state>READY</state>\
                    <startTime>1603123200</startTime>\
                    <formattedStartTime>10/19/2020 12:00:00 EDT</formattedStartTime>\
                    <endTime>1603123200</endTime>\
                    <formattedEndTime>10/19/2020 12:00:00 EDT</formattedEndTime>\
                    <objectsExamined>5</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ErasureCodingService</name>\
                    <state>READY</state>\
                    <startTime>1610470800</startTime>\
                    <formattedStartTime>1/12/2021 12:00:00 EST</formattedStartTime>\
                    <endTime>1610514019</endTime>\
                    <formattedEndTime>1/13/2021 0:00:19 EST</formattedEndTime>\
                    <objectsExamined>70829122</objectsExamined>\
                    <objectsExaminedPerSecond>1638.84</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>DispositionPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ScavengingPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610254804</startTime>\
                    <formattedStartTime>1/10/2021 0:00:04 EST</formattedStartTime>\
                    <endTime>1610305205</endTime>\
                    <formattedEndTime>1/10/2021 14:00:05 EST</formattedEndTime>\
                    <objectsExamined>4338961</objectsExamined>\
                    <objectsExaminedPerSecond>86.09</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>SNodeCompressionEncryptionValidationService</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ProtectionPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610254804</startTime>\
                    <formattedStartTime>1/10/2021 0:00:04 EST</formattedStartTime>\
                    <endTime>1610305252</endTime>\
                    <formattedEndTime>1/10/2021 14:00:52 EST</formattedEndTime>\
                    <objectsExamined>9309682</objectsExamined>\
                    <objectsExaminedPerSecond>184.54</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>DuplicateElimination</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514000</startTime>\
                    <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>LOW</performanceLevel>\
                    <objectsExamined>11176935</objectsExamined>\
                    <objectsExaminedPerSecond>313.15</objectsExaminedPerSecond>\
                    <objectsServiced>4</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>CompressionPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610463600</startTime>\
                    <formattedStartTime>1/12/2021 10:00:00 EST</formattedStartTime>\
                    <endTime>1610478000</endTime>\
                    <formattedEndTime>1/12/2021 14:00:00 EST</formattedEndTime>\
                    <objectsExamined>122806</objectsExamined>\
                    <objectsExaminedPerSecond>8.53</objectsExaminedPerSecond>\
                    <objectsServiced>118215</objectsServiced>\
                    <objectsServicedPerSecond>8.21</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
            </services>\
        </node>\
        <node>\
            <nodeNumber>30</nodeNumber>\
            <frontendIpAddresses>\
                <ipAddress>172.18.120.30</ipAddress>\
            </frontendIpAddresses>\
            <managementIpAddresses>\
                <ipAddress>172.18.200.250</ipAddress>\
            </managementIpAddresses>\
            <ipAddress>172.120.24.30</ipAddress>\
            <services>\
                <service>\
                    <name>StorageTieringService</name>\
                    <state>READY</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>RetirementPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>MigrationVerificationPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ContentVerification</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514011</startTime>\
                    <formattedStartTime>1/13/2021 0:00:11 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>HIGH</performanceLevel>\
                    <objectsExamined>3936796</objectsExamined>\
                    <objectsExaminedPerSecond>110.33</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>GarbageCollection</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514011</startTime>\
                    <formattedStartTime>1/13/2021 0:00:11 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>HIGH</performanceLevel>\
                    <objectsExamined>2284441</objectsExamined>\
                    <objectsExaminedPerSecond>64.02</objectsExaminedPerSecond>\
                    <objectsServiced>807593</objectsServiced>\
                    <objectsServicedPerSecond>22.63</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>MigrationPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>CapacityBalancePolicy</name>\
                    <state>READY</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>SNodeCapacityBalanceService</name>\
                    <state>READY</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ErasureCodingService</name>\
                    <state>READY</state>\
                    <startTime>1610470800</startTime>\
                    <formattedStartTime>1/12/2021 12:00:00 EST</formattedStartTime>\
                    <endTime>1610514000</endTime>\
                    <formattedEndTime>1/13/2021 0:00:00 EST</formattedEndTime>\
                    <objectsExamined>107165113</objectsExamined>\
                    <objectsExaminedPerSecond>2480.67</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>DispositionPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ScavengingPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610254800</startTime>\
                    <formattedStartTime>1/10/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>1610305205</endTime>\
                    <formattedEndTime>1/10/2021 14:00:05 EST</formattedEndTime>\
                    <objectsExamined>5184954</objectsExamined>\
                    <objectsExaminedPerSecond>102.87</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>SNodeCompressionEncryptionValidationService</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ProtectionPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610254814</startTime>\
                    <formattedStartTime>1/10/2021 0:00:14 EST</formattedStartTime>\
                    <endTime>1610305205</endTime>\
                    <formattedEndTime>1/10/2021 14:00:05 EST</formattedEndTime>\
                    <objectsExamined>8951448</objectsExamined>\
                    <objectsExaminedPerSecond>177.64</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>DuplicateElimination</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514011</startTime>\
                    <formattedStartTime>1/13/2021 0:00:11 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>LOW</performanceLevel>\
                    <objectsExamined>11173120</objectsExamined>\
                    <objectsExaminedPerSecond>313.14</objectsExaminedPerSecond>\
                    <objectsServiced>1</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>CompressionPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610463600</startTime>\
                    <formattedStartTime>1/12/2021 10:00:00 EST</formattedStartTime>\
                    <endTime>1610478000</endTime>\
                    <formattedEndTime>1/12/2021 14:00:00 EST</formattedEndTime>\
                    <objectsExamined>145556</objectsExamined>\
                    <objectsExaminedPerSecond>10.11</objectsExaminedPerSecond>\
                    <objectsServiced>140224</objectsServiced>\
                    <objectsServicedPerSecond>9.74</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
            </services>\
        </node>\
        <node>\
            <nodeNumber>31</nodeNumber>\
            <frontendIpAddresses>\
                <ipAddress>172.18.120.31</ipAddress>\
            </frontendIpAddresses>\
            <managementIpAddresses>\
                <ipAddress>172.18.200.251</ipAddress>\
            </managementIpAddresses>\
            <ipAddress>172.120.24.31</ipAddress>\
            <services>\
                <service>\
                    <name>StorageTieringService</name>\
                    <state>READY</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>RetirementPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>MigrationVerificationPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ContentVerification</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514000</startTime>\
                    <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>HIGH</performanceLevel>\
                    <objectsExamined>3774892</objectsExamined>\
                    <objectsExaminedPerSecond>105.76</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>GarbageCollection</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514000</startTime>\
                    <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>HIGH</performanceLevel>\
                    <objectsExamined>1761262</objectsExamined>\
                    <objectsExaminedPerSecond>49.35</objectsExaminedPerSecond>\
                    <objectsServiced>725392</objectsServiced>\
                    <objectsServicedPerSecond>20.32</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>MigrationPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>CapacityBalancePolicy</name>\
                    <state>READY</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>SNodeCapacityBalanceService</name>\
                    <state>READY</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ErasureCodingService</name>\
                    <state>READY</state>\
                    <startTime>1610470800</startTime>\
                    <formattedStartTime>1/12/2021 12:00:00 EST</formattedStartTime>\
                    <endTime>1610514000</endTime>\
                    <formattedEndTime>1/13/2021 0:00:00 EST</formattedEndTime>\
                    <objectsExamined>75158962</objectsExamined>\
                    <objectsExaminedPerSecond>1739.79</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>DispositionPolicy</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ScavengingPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610254800</startTime>\
                    <formattedStartTime>1/10/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>1610305205</endTime>\
                    <formattedEndTime>1/10/2021 14:00:05 EST</formattedEndTime>\
                    <objectsExamined>4749161</objectsExamined>\
                    <objectsExaminedPerSecond>94.22</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>SNodeCompressionEncryptionValidationService</name>\
                    <state>DISABLED</state>\
                    <startTime>-1</startTime>\
                    <endTime>-1</endTime>\
                    <objectsExamined>0</objectsExamined>\
                    <objectsExaminedPerSecond>0.0</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>ProtectionPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610254800</startTime>\
                    <formattedStartTime>1/10/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>1610305200</endTime>\
                    <formattedEndTime>1/10/2021 14:00:00 EST</formattedEndTime>\
                    <objectsExamined>9962463</objectsExamined>\
                    <objectsExaminedPerSecond>197.67</objectsExaminedPerSecond>\
                    <objectsServiced>0</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>DuplicateElimination</name>\
                    <state>RUNNING</state>\
                    <startTime>1610514000</startTime>\
                    <formattedStartTime>1/13/2021 0:00:00 EST</formattedStartTime>\
                    <endTime>-1</endTime>\
                    <performanceLevel>LOW</performanceLevel>\
                    <objectsExamined>11151418</objectsExamined>\
                    <objectsExaminedPerSecond>312.43</objectsExaminedPerSecond>\
                    <objectsServiced>3</objectsServiced>\
                    <objectsServicedPerSecond>0.0</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
                <service>\
                    <name>CompressionPolicy</name>\
                    <state>READY</state>\
                    <startTime>1610463600</startTime>\
                    <formattedStartTime>1/12/2021 10:00:00 EST</formattedStartTime>\
                    <endTime>1610478000</endTime>\
                    <formattedEndTime>1/12/2021 14:00:00 EST</formattedEndTime>\
                    <objectsExamined>124682</objectsExamined>\
                    <objectsExaminedPerSecond>8.66</objectsExaminedPerSecond>\
                    <objectsServiced>120046</objectsServiced>\
                    <objectsServicedPerSecond>8.34</objectsServicedPerSecond>\
                    <objectsUnableToService>0</objectsUnableToService>\
                    <objectsUnableToServicePerSecond>0.0</objectsUnableToServicePerSecond>\
                </service>\
            </services>\
        </node>\
    </nodes>\
</serviceStatistics>\
```\
\
### smtpProtocol\
\
The `smtpProtocol` data type describes the smtp resource for HCPnamespaces.\
\
## Properties\
\
The table below describes the properties included in the `smtpProtocol` data type.\
\
| Property | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| emailFormat | String | Specifies the format for email objects added through the SMTP protocol. Valid values are:<br>- .eml<br>- .mbox<br>The default is .eml. |  |\
| emailLocation | String | Specifies the path for the directory in which to store email objects added through the SMTP protocol. This is the path relative to the root of the namespace (that is, rest, data, or fcfs\_data). Start and end the path with a forward slash (/), like this:<br>/email/company\_all/<br>The default is /email/.<br>Directory names are case sensitive. | If the specified directory does not exist, HCP creates it. |\
| enabled | Boolean | Specifies whether the SMTP protocol is enabled for the namespace. Valid values are:<br>true<br> SMTP is enabled.false<br> SMTP is disabled.<br>The default is false. |  |\
| ipSettings | ipSettings | Specifies which IP addresses can and cannot access the namespace through the SMTP protocol. |  |\
| separateAttachments | Boolean | Specifies whether HCP should store attachments separately from the email they accompany. Valid values are:<br>true<br> Store attachments separately.false<br> Do not store attachments separately.<br>The default is false. |  |\
\
## Example\
\
Here’s an XML example of the `smtpProtocol` data type:\
\
```\
<smtpProtocol>\
    <emailFormat>.eml</emailFormat>\
    <emailLocation>/finance/email/<emailLocation>\
    <enabled>true</enabled>\
    <ipSettings>\
         <allowAddresses>\
             <ipAddress>192.168.45.213</ipAddress>\
         </allowAddresses>\
         <denyAddresses/>\
    </ipSettings>\
    <separateAttachments>false</separateAttachments>\
</smtpProtocol>\
```\
\
### Specifying retention values\
\
These properties require you to specify a retention value:\
\
- The `retentionDefault` property of the `complianceSettings` data type. The value of this property can be a special value, offset, retention class, or fixed date.\
- The `minimumRetentionAfterInitialUnspecified` property of the `complianceSettings` data type. The value of this property can be an offset.\
- The value property of the `retentionClass` data type. The value of this property can be a special value or an offset.\
\
## Specifying a special value\
\
To specify a special value as a retention value, use one of these:\
\
0\
Deletion AllowedAllows the object to be deleted at any time.-1\
Deletion ProhibitedPrevents the object from ever being deleted by means of a normal delete operation. If the namespace is in enterprise mode, however, the object can be deleted by means of a privileged delete operation.-2\
Initial UnspecifiedPrevents the object from being deleted but allows its retention setting to be changed.\
\
These values are not case sensitive.\
\
## Specifying an offset\
\
To specify an offset as a retention value, use a standard expression that conforms to this syntax:\
\
```\
^A([+-]\d+y)?([+-]\d+M)?([+-]\d+w)?([+-]\d+d)?([+-]\d+h)?([+-]\d+m)?([+-]\d+s)?$\
```\
\
The list below explains this syntax.\
\
^\
Start of the expression$\
End of the expression( )\
Sequence of terms treated as a single term?\
Indicator that the preceding term is optional\[ \]\
Group of alternatives, exactly one of which must be used+\
Plus-\
MinusA\
The time at which the object was added to the repository/d+\
An integer in the range 0 (zero) through 9,999y\
YearsM\
MonthsW\
WeeksD\
Daysh\
Hoursm\
Minutess\
Seconds\
\
The time measurements included in an expression must go from the largest unit to the smallest (that is, in the order in which they appear in the syntax). These measurements are case sensitive. You can omit measurements that have value of zero.\
\
Here are some offset examples:\
\
```\
A+100y\
```\
\
```\
A+20d-5h\
```\
\
```\
A+2y+1d\
```\
\
## Specifying a retention class\
\
To specify a retention class as a retention value, use this format:\
\
```\
C+retention-class-name\
```\
\
For example, if the name of the retention class is HlthReg-107, specify the property value as:\
\
```\
C+HlthReg-107\
```\
\
The retention class you specify must already exist.\
\
## Specifying a fixed date\
\
To specify a fixed date as a retention value, you can use either of these formats:\
\
- Time in seconds since January 1, 1970, at 00:00:00. For example:\
\
\
```\
1514678400\
```\
\
\
The calendar date that corresponds to 1514678400 is Sunday, December 31, 2017, at 00:00:00 EST.\
\
- Date and time in this ISO 8601 format:\
\
\
```\
yyyy-MM-ddThh:mm:ssZ\
```\
\
\
Z represents the offset from UTC and is specified as:\
\
\
```\
(+|-)hhmm\
```\
\
\
For example: 2017-12-31T00:00:00-0500\
\
\
If you specify certain invalid dates, HCP automatically adjusts the value to make a real date. For example, if you specify a default retention setting of 2017-11-33T00:00:00-0500, which is three days past the end of November, objects added to the namespace get a retention setting of 2017-12-03T00:00:00-0500.\
\
### statistics (data type for replication link statistics property)\
\
The `statistics` data type in this section describes the `statistics` property of the `link` data type.\
\
## Replication link statistics data type properties\
\
The following table describes the properties included in the `statistics` data type that describes the `statistics` property of the `link` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| bytesPending | Long | Specifies the approximate amount of data currently waiting to be replicated from the local system to the remote system for the replication link, in bytes. This value is the sum of the amounts of data waiting to be sent in each HCP namespace included in the link. This value does not include data in the default namespace.<br>If the local system is the replica for an active/passive link, the value of this property during replication is zero. If the local system is the primary system for an active/passive link, the value of this property during data recovery is zero. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| bytesPendingRemote | Long | Specifies the approximate amount of data currently waiting to be replicated from the remote system for the replication link to the local system, in bytes. This value is the sum of the amounts of data waiting to be sent in each HCP namespace included in the link. This value does not include data in the default namespace.<br>For an active/passive link, the value of this property is always zero. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| bytesPerSecond | Double | Specifies the current rate of data transmission on the replication link, in bytes per second.<br>For an active/active link, the value of this property is the rate of data transmission from the local system to the remote system. For an active/passive link, the value of this property is the rate of data transmission during replication or recovery, whichever is happening at the time. In any case, the data transmission rate is cumulative for all the HCP namespaces and default-namespace directories included in the link. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| bytesReplicated | Long | Specifies the total number of bytes of data replicated from the local system to the remote system for the replication link since the link was created. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| errors | Long | Specifies the total number of errors that have occurred during replication or recovery from the local system to the remote system for the replication link since the link was created. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| errorsPerSecond | Double | Specifies the current rate of errors on the replication link per second.<br>For an active/active link, the value of this property is the error rate for replication from the local system to the remote system for the link. For an active/passive link, the value of this property is the error rate during replication or recovery, whichever is happening at the time. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| objectsPending | Long | Specifies the approximate number of objects currently waiting to be replicated from the local system to the remote system for the replication link. This value is the sum of the numbers of objects waiting to be sent in each HCP namespace included in the link. This value does not include objects in the default namespace.<br>If the local system is the replica for an active/passive link, the value of this property during replication is zero. If the local system is the primary system for an active/passive link, the value of this property during data recovery is zero. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| objectsPendingRemote | Long | Specifies the approximate number of objects currently waiting to be replicated from the remote system for the replication link to the local system. This value is the sum of the numbers of objects waiting to be sent in each HCP namespace included in the link. This value does not include objects in the default namespace.<br>For an active/passive link, the value of this property is always zero. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| objectsReplicated | Long | Specifies the total number of objects replicated from the local system to the remote system for the replication link since the link was created. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| objectsReplicatedAfterVerification | Long | Specifies the number of objects replicated by the Replication Verification service. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| objectsVerified | Long | Specifies the number of objects analyzed by the Replication Verification service. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| operationsPerSecond | Double | Specifies the current rate of operations on the replication link per second. An operation is the replication of any of these:<br>- An object, directory, symbolic link, metadata change, or object deletion<br>- An HCP tenant or HCP namespace or the modification or deletion of an HCP tenant or HCP namespace<br>- For HCP tenants only, the creation, modification, or deletion of a user account<br>- The creation, modification, or deletion of a retention class<br>- The creation, modification, or deletion of a content class<br>- A tenant log message<br>For an active/active link, the value of this property is the operation rate for replication from the local system to the remote system for the link. For an active/passive link, the value of this property is the operation rate during replication or recovery, whichever is happening at the time. In any case, the operation rate is cumulative for all the tenants being replicated or recovered on the link. | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| upToDateAsOfMillis | Long | Specifies, in milliseconds since January 1, 1970, at 00:00:00 UTC:<br>- For an active/active link, the date and time before which configuration changes and changes to namespace content are guaranteed to be synchronized in both directions between the two systems involved in the link<br>- For an active/passive link, the date and time before which configuration changes and changes to namespace content are guaranteed to have been replicated or recovered on the link, as applicable | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
| upToDateAsOfString | String | Specifies, as a datetime string:<br>- For an active/active link, the date and time before which configuration changes and changes to namespace content are guaranteed to be synchronized in both directions between the two systems involved in the link<br>- For an active/passive link, the date and time before which configuration changes and changes to namespace content are guaranteed to have been replicated or recovered on the link, as applicable<br>The datetime string has this format:<br>yyyy-MM-ddThh:mm:ssZ<br>In this format, hh is the hour on a 24-hour clock, and Z represents the offset from UTC and is specified as:<br>(+\|-)hhmm<br>For example:<br>2017-03-18T10:47:59-0400 | This property is not valid on a PUT or POST request for a replication link. It is returned only by a verbose GET request. |\
\
## Example\
\
Here’s an XML example of the `statistics` data type that describes the `statistics` property of the `link` data type.\
\
```\
<statistics>\
     <upToDateAsOfString>2019-07-12T10:47:59-0400</upToDateAsOfString>\
     <upToDateAsOfMillis>1395154079640</upToDateAsOfMillis>\
     <bytesPending>189027593061</bytesPending>\
     <bytesPendingRemote>319740</bytesPendingRemote>\
     <bytesReplicated>72254119306967</bytesReplicated>\
     <bytesPerSecond>56215390</bytesPerSecond>\
     <objectsPending>534</objectsPending>\
     <objectsPendingRemote>2</objectsPendingRemote>\
     <objectsReplicated>295661</objectsReplicated>\
     <operationsPerSecond>119.1</operationsPerSecond>\
     <errors>0</errors>\
     <errorsPerSecond>0.0</errorsPerSecond>\
     <objectsVerified>200000</objectsVerified>\
     <objectsReplicatedAfterVerification>0</objectsReplicatedAfterVerification>\
</statistics>\
```\
\
### statistics (data type for tenant and namespace statistics resources)\
\
The `statistics` data type in this section describes the `statistics` resource for tenants and namespaces.\
\
## Properties\
\
The table below describes the properties included in the `statistics` data type that describes the `statistics` resource for tenants and namespaces.\
\
| Property | Data Type | Description |\
| --- | --- | --- |\
| customMetadataCount | Long | Specifies the number of custom metadata files stored in a given namespace or in all the namespaces owned by a given tenant. |\
| customMetadataSize | Long | Specifies the total number of bytes of custom metadata stored in a given namespace or in all the namespaces owned by a given tenant. |\
| ingestedVolume | Long | Specifies the total size of the stored data and custom metadata, in bytes, before it was added to a given namespace or to all the namespaces owned by a given tenant. |\
| objectCount | Long | Specifies the number of objects, including old versions of objects, in a given namespace or in all the namespaces owned by a given tenant.<br>Each multipart object counts as a single object. Objects that are in the process of being created by multipart uploads are not included in the object count.<br>The object count does not include object versions that are delete markers or delete records. |\
| shredCount | Long | Specifies thetotal number of these items waiting to be shredded in a given namespace or in all the namespaces owned by a given tenant: objects, old versions of objects, parts of multipart objects, parts of old multipart versions of objects, replaced parts of multipart uploads, parts of aborted multipart uploads, unused parts of completed multipart uploads, and transient parts created during the processing of certain multipart upload operations. |\
| shredSize | Long | Specifies the total number of bytes of object, object version, and part data and metadata waiting to be shredded in a given namespace or in all the namespaces owned by a given tenant. |\
| storageCapacityUsed | Long | Specifies the total number of bytes occupied by stored data in the given namespace or in all the namespaces owned by the given tenant. This includes object data, metadata, and any redundant data required to satisfy the applicable service plans. |\
\
## Example\
\
Here’s an XML example of the `statistics` data type that describes the `statistics` resource for tenants and namespaces:\
\
```\
<statistics>\
    <customMetadataCount>5</customMetadataCount>\
    <customMetadataSize>3276</customMetadataSize>\
    <objectCount>1616</objectCount>\
    <shredCount>0</shredCount>\
    <shredSize>0</shredSize>\
    <storageCapacityUsed>143892480</storageCapacityUsed>\
</statistics>\
```\
\
### Support access credentials data type\
\
Use the `supportAccessCredentials` data type to retrieve Support access credentials for an HCP system.\
\
Note: A system-level user account with the monitor, administrator, security, or service role is required to retrieve Hitachi Vantara Support access credentials for an HCP system.\
\
\
## supportAccessCredentials data type properties\
\
The following table describes the properties included in the `supportAccessCredentials` data type.\
\
| Property name | Data type | Description |\
| --- | --- | --- |\
| applyTimeStamp | Integer | Time that the exclusive Hitachi Vantara Support Access Credentials key was applied to the HCP system |\
| createTimeStamp | String | Time that the exclusive Hitachi Vantara Support Access Credentials key was created |\
| type | String | Valid values:<br> <br>- `Default`<br>- `Exclusive` |\
| defaultKeyType | String | Default key type that shipped with the HCP system |\
| serialNumberFromPackage | Integer | Serial number of the installed Hitachi Vantara Support Access Credentials key |\
\
Here is an XML example of the `supportAccessCredentials` data type returned for system-specific exclusive Hitachi Vantara Support access credentials:\
\
```\
<supportAccessCredentials>\
     <applyTimeStamp>1599143327</applyTimeStamp>\
     <createTimeStamp>1597699675</createTimeStamp>\
     <type>Exclusive</type>\
     <defaultKeyType>Arizona</defaultKeyType>\
     <serialNumberFromPackage>425999</serialNumberFromPackage>\
</supportAccessCredentials>\
```\
\
Here is an XML example of the `supportAccessCredentials` data type returned for default Hitachi Vantara Support access credentials:\
\
\
```\
<supportAccessCredentials>\
     <type>Default</type>\
     <defaultKeyType>Arizona</defaultKeyType>\
</supportAccessCredentials>\
```\
\
### tenant (data type for replication link content tenant resource)\
\
The `tenant` data type in this section describes the `tenant-name` resource for replication link content.\
\
## Replication link content tenant data type properties\
\
The following table describes the properties included in the `tenant` data type that describes the `tenant-name` resource for replication link content.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| bytesPending | Long | For an active/active link, an outbound active/passive link during replication, or an inbound active/passive link during data recovery, specifies the approximate amount of data in the tenant's namespaces that is currently waiting to be replicated from the local system to the remote system for the replication link, in bytes.<br>For an outbound link during data recovery or for an inbound link during replication, the value of this property is always zero. This value is also always zero for the default tenant. |  |\
| bytesPendingRemote | Long | Specifies the approximate amount of data in the tenant's namespaces that is currently waiting to be replicated from the remote system for the replication link to the local system, in bytes.<br>For an active/passive link, the value of this property is always zero. This value is also always zero for the default tenant. |  |\
| objectsPending | Long | For an active/active link, an outbound active/passive link during replication, or an inbound active/passive link during data recovery, specifies the approximate number of objects in the tenant's namespaces that are currently waiting to be replicated from the local system to the remote system for the replication link.<br>For an outbound link during data recovery or for an inbound link during replication, the value of this property is always zero. This value is also always zero for the default tenant. |  |\
| objectsPendingRemote | Long | Specifies the approximate number of objects in the tenant's namespaces that are currently waiting to be replicated from the remote system for the replication link to the local system.<br>For an active/passive link, the value of this property is always zero. This value is also always zero for the default tenant. |  |\
| status | String | Specifies the status of activity for the tenant on the replication link. Possible values are:<br>AUTO\_PAUSED<br> HCP automatically paused replication or recovery of the tenant.<br> PAUSED<br> A user paused replication or recovery of the tenant.REPLICATING<br> Replication or recovery of the tenant is proceeding normally. |  |\
| upToDateAsOfMillis | Long | Specifies, in milliseconds since January 1, 1970, at 00:00:00 UTC, the date and time before which configuration changes and changes to namespace content for all the replicating namespaces owned by the tenant are guaranteed to have been replicated from the local system to the remote system for the replication link. |  |\
| upToDateAsOfString | String | Specifies, as a datetime string, the date and time before which configuration changes and changes to namespace content for all the replicating namespaces owned by the tenant are guaranteed to have been replicated from the local system to the remote system for the replication link.<br>The datetime string has this format:<br>`yyyy-MM-ddThh:mm:ssZ`<br>In this format, hh is the hour on a 24-hour clock, and<br>`Z` represents the offset from UTC, in this format:<br>( `+` \| `-`) `hhmm`<br>For example:<br>2017-03-18T10:47:59-0400 |  |\
\
## Example\
\
Here’s an XML example of the `tenant` data type that describes the `tenant-name` resource for replication link content; the properties shown are those that are returned by a GET request for a tenant in an active/active link:\
\
```\
<tenant>\
    <status>REPLICATING</status>\
    <upToDateAsOfString>2017-03-19T17:45:15-0400</upToDateAsOfString>\
    <upToDateAsOfMillis>1395265515303</upToDateAsOfMillis>\
    <objectsPending>196</objectsPending>\
    <bytesPending>46347338966</bytesPending>\
    <objectsPendingRemote>14</objectsPendingRemote>\
    <bytesPendingRemote>735856</bytesPendingRemote>\
</tenant>\
```\
\
## Query parameters for replication link content tenant actions\
\
To perform actions on tenants in replication links, you use these query parameters:\
\
pause\
Pauses replication or recovery for the tenant on the replication link, as applicable.resume\
Resumes replication or recovery for the tenant on the replication link, as applicable.\
\
You use these query parameters with a POST request against the replication link content tenant resource. You cannot include a request body with this request.\
\
Here’s a sample POST request that pauses replication activity for the Finance tenant on the replication link named MA-CA:\
\
```\
curl -k -iX POST\
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"\
    "https://admin.hcp-ma.example.com:9090/mapi/services/replication/links/\
       MA-CA/content/tenants/Finance?pause"\
```\
\
### tenant (data type for tenant resource)\
\
The tenant data type in this section describes the `tenants` resource.\
\
When you create an HCP tenant, you need to specify query parameters that define the initial user account for the tenant. When you create the default tenant, you need to specify query parameters that further define the default namespace.\
\
## Properties\
\
The table below describes the properties included in the `tenant` data type that describes the `tenants` resource.\
\
| Property | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| administrationAllowed | Boolean | For an HCP tenant, specifies whether system-level users have administrative access to the tenant. Valid values are:<br>true<br> System-level users have administrative access to the tenant.false<br> System-level users do not have administrative access to the tenant.<br>The default is false. | This property is not valid for the default tenant. |\
| authenticationTypes | String | For an HCP tenant, lists the types of user authentication supported by the tenant. Possible authentication types are:<br>LOCAL<br> <br> The tenant supports local authentication by HCP.<br> RADIUS<br> The tenant supports remote authentication by RADIUS servers.EXTERNAL<br> The tenant supports remote authentication by AD. | This property is not valid on a POST request. It is returned only by a verbose GET request.<br>In XML, each listed authentication type is the value of an element named `authenticationType`. In JSON, the name in the name/value pair that lists the authentication types is `authenticationType`.<br>This property is not valid for the default tenant. |\
| complianceConfiguration Enabled | Boolean | For an HCP tenant, specifies whether the tenant can set the retention mode of the namespaces it owns. Possible values are:<br>true<br> The tenant can set the retention mode.false<br> The tenant cannot set the retention mode. | This property is not valid on a POST request. It is returned only by a verbose GET request.<br>This property is not valid for the default tenant.<br>In enterprise mode, privileged deletes are allowed, retention class durations can be shortened, and retention classes can be deleted. In compliance mode, these activities are not allowed. |\
| creationTime | String | Specifies the date and time at which the tenant was created, in this ISO 8601 format:<br>`yyyy-MM-ddThh:mm:ssZ`<br>`Z` represents the offset from UTC, in this format:<br>( `+` \| `-`) `hhmm`<br>For example:<br>2017-02-09T19:26:32-0500 | This property is not valid on a POST request. It is returned only by a verbose GET request. |\
| fullyQualifiedName | String | Specifies the fully qualified hostname of the tenant. | This property is not valid on a POST request. It is returned only by a verbose GET request. |\
| hardQuota | String | For an HCP tenant, specifies the total amount of space available to the tenant for allocation to its namespaces.<br>Possible values are decimal numbers with up to two places after the period, followed by a space, followed by MB, GB, or TB (for example, 10.25 TB). The minimum value is 1 (one) for GB and .01 for TB. | This property is not valid on a POST request. It is returned only by a verbose GET request.<br>This property is not valid for the default tenant. |\
| id | String | Specifies the system-supplied unique ID for the tenant. HCP generates this ID automatically when you create a tenant. | This property is not valid on a POST request. It is returned only by a verbose GET request. |\
| maxNamespacesPer User | integer | For an HCP tenant, specifies the maximum number of namespaces that can be owned at one time by any given user.<br>Valid values are zero through 10,000. The default is 100. | This property is not valid for the default tenant. |\
| name | String | Specifies the name of the tenant. HCP derives the hostname for the tenant from this name. The hostname is used in URLs for access to the tenant and its namespaces.<br>The name of the default tenant is always Default. | This property is not valid on a POST request. It is returned only by a verbose GET request for an HCP tenant.<br>This property is not returned by any GET request for the default tenant.<br>The tenant name is used in the URL for access to the tenant. |\
| namespaceQuota | String | For an HCP tenant, specifies the number of namespaces HCP reserves for the tenant out of the total number of namespaces the system can have. This is also maximum number of namespaces the tenant can own at any given time.<br>Possible values are:<br>- Integers in the range 1 (one) through the current number of namespaces available for allocation. The number of available namespaces is equal to 10,000 minus the number of namespaces currently allocated to HCP tenants, minus the number of namespaces currently owned by HCP tenants without a quota, minus one for the default namespace, if it exists. If any tenants are above quota, the number of excess namespaces is also subtracted from the number of available namespaces.<br>- None | This property is not valid on a POST request. It is returned only by a verbose GET request.<br>This property is not valid for the default tenant. |\
| replicationConfiguration Enabled | Boolean | For an HCP tenant, specifies whether the tenant is eligible for replication. Possible values are:<br>true<br> The tenant is eligible for replication.false<br> The tenant is not eligible for replication. | This property is not valid on a POST request. It is returned only by a verbose GET request.<br>This property is not valid for the default tenant. If the HCP system supports replication, directories in the default namespace are automatically eligible for replication. |\
| searchConfiguration Enabled | Boolean | For an HCP tenant, specifies whether the tenant can enable and disable search for the namespaces it owns. Possible values are:<br>true<br> The tenant can enable and disable search for its namespaces.false<br> The tenant cannot enable or disable search for its namespaces. | This property is not valid on a POST request. It is returned only by a verbose GET request.<br>This property is not valid for the default tenant. |\
| snmpLoggingEnabled | Boolean | Specifies whether tenant log messages are sent to the SNMP managers specified at the HCP system level. Valid values are:<br>true<br> Tenant log messages are sent to SNMP managers.false<br> Tenant log messages are not sent to SNMP managers.<br>The default is false. |  |\
| softQuota | Integer | For an HCP tenant, specifies the percentage point at which HCP notifies the tenant that free storage space is running low.<br>Possible values are integers in the range zero through 100. | This property is not valid on a POST request. It is returned only by a verbose GET request.<br>This property is not valid for the default tenant. |\
| syslogLoggingEnabled | Boolean | Specifies whether tenant log messages are sent to the syslog servers specified at the HCP system level. Valid values are:<br>true<br> Tenant log messages are sent to syslog servers.false<br> Tenant log messages are not sent to syslog servers.<br>The default is false. |  |\
| tenantVisibleDescription | String | Specifies the tenant-level description of the tenant. This description is optional. The default is no description.<br>To remove a tenant-level description from an existing tenant, specify the `tenantVisibleDescription` property with no value. |  |\
| versioningConfiguration Enabled | Boolean | For an HCP tenant, specifies whether the tenant's namespaces can have versioning enabled. Possible values are:<br>true<br> The tenant's namespaces can have versioning enabled.false<br> The tenant's namespaces cannot have versioning enabled. | This property is not valid on a POST request. It is returned only by a verbose GET request.<br>This property is not valid for the default tenant. |\
\
## Example\
\
Here’s an XML example of the `tenant` data type that describes the `tenants` resource; the properties shown are those that are returned in response to a verbose GET request for an HCP tenant where the request is made using a tenant-level user account that includes the administrator role:\
\
```\
<tenant>\
    <administrationAllowed>true</administrationAllowed>\
    <authenticationTypes>\
        <authenticationType>LOCAL</authenticationType>\
        <authenticationType>RADIUS</authenticationType>\
    </authenticationTypes>\
    <complianceConfigurationEnabled>true</complianceConfigurationEnabled>\
    <creationTime>2017-02-09T09:11:17-0500</creationTime>\
    <fullyQualifiedName>Finance.hcp.example.com</fullyQualifiedName>\
    <hardQuota>100 GB</hardQuota>\
    <maxNamespacesPerUser>5</maxNamespacesPerUser>\
    <name>Finance</name>\
    <namespaceQuota>5</namespaceQuota>\
    <replicationConfigurationEnabled>true</replicationConfigurationEnabled>\
    <snmpLoggingEnabled>false</snmpLoggingEnabled>\
    <searchConfigurationEnabled>true</searchConfigurationEnabled>\
    <softQuota>90</softQuota>\
    <syslogLoggingEnabled>true</syslogLoggingEnabled>\
    <tenantVisibleDescription>Please see Lee Green for any questions about this\
        tenant and its namespaces.</tenantVisibleDescription>\
    <id>4420f62f-3f63-43ab-a3cd-0bcf1f399daf</id>\
    <versioningConfigurationEnabled>true</versioningConfigurationEnabled>\
</tenant>\
```\
\
## Query parameters for creating tenants\
\
When you create a tenant, you need to specify query parameters on the resource URL. These parameters differ for HCP tenants and the default tenant.\
\
## HCP tenant query parameters\
\
When you create an HCP tenant, HCP automatically creates the initial user or group account for the tenant, depending on which query parameters you include in the PUT request.\
\
## Creating an initial user account\
\
To create a tenant with an initial user account, you use these query parameters, which correspond to user account properties with the same name:\
\
username\
This parameter is required when you create a tenant. The user name you specify is also used as the full name for the user account.password\
This parameter is required when you create a tenant.forcePasswordChange\
This parameter is optional when you create a tenant. The default is false.\
\
The user account that’s created:\
\
\
- Is enabled\
- Is locally authenticated\
- Has only the security role\
- Has no data access permissions\
- Has no description\
\
The username, password, and forcePasswordChange query parameters are valid only when you create an HCP tenant and only if you enable local authentication for the tenant in the same request. They are not valid on a request to modify a tenant.\
\
## Creating an initial group account\
\
To create the tenant with an initial group account, you use the initialSecurityGroup query parameter. The value of this parameter must be the name or SID of an AD group defined in the AD forest supported by HCP. You can specify the name in either of these formats:\
\
```\
group-name\
```\
\
```\
group-name@ad-domain-name\
```\
\
If you omit the domain name, HCP uses the AD domain specified in the system configuration.\
\
Be sure to use the second format if a group with the specified name exists in more than one domain in the AD forest or if the group name looks like a SID.\
\
The group account that’s created:\
\
- Has only the security role\
- Has no data access permissions\
\
The initialSecurityGroup query parameter is valid only when you create an HCP tenant and only if you enable AD authentication for the tenant in the same request. It is not valid on a request to modify a tenant.\
\
## Default tenant query parameters\
\
When you create the default tenant, HCP automatically creates the default namespace. To provide information about this namespace, you use these query parameters, which correspond to namespace properties with the same name:\
\
enterpriseMode\
This parameter is required when you create the default tenant.hashScheme\
This parameter is required when you create the default tenant.searchEnabled\
\
This parameter is optional when you create the default tenant. The default is false. (By default, if you specify searchEnabled=true, search indexing is enabled. Otherwise, search indexing is disabled by default.)\
servicePlan\
This parameter is optional when you create the default tenant. The default is Default.\
\
These query parameters are valid only when you create the default tenant. They are not valid on requests to modify the tenant.\
\
### tenantCandidate\
\
The `tenantCandidate` data type describes the `tenantCandidates` property of the `tenantCandidates` and `tenantConflictingCandidates` resources for erasure coding topologies.\
\
## tenantCandidate data type properties\
\
The following table describes the properties included in the `tenantCandidate` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| hcpSystems | List | Lists the HCP systems on which the tenant exists. Each system is identified by the fully qualified name of the domain associated with the \[hcp\_system\] network on that system. | This property is returned only by a verbose GET request.<br>In XML, the element that identifies each system is name. In JSON, the name in the name/value pair that lists the systems is name. |\
| name | String | Specifies the name of the tenant. |  |\
| uuid | String | Specifies the unique ID for the tenant. | This property is returned only by a verbose GET request. |\
\
## Example\
\
Here's an XML example of the `tenantCandidate` data type; the properties shown are those that are returned in response to a verbose GET request:\
\
```\
<tenantCandidate>\
    <hcpSystems>\
        <name>hcp-eu.example.com</name>\
        <name>hcp-us.example.com</name>\
    </hcpSystems>\
    <name>finance</name>\
    <uuid>838cd575-0f94-489a-8f94-f36c1337c446</uuid>\
</tenantCandidate>\
```\
\
### tenantCandidates\
\
The `tenantCandidates` data type describes the `tenantCandidates` and `tenantConflictingCandidates` resources for erasure coding topologies.\
\
## tenantCandidates data type property\
\
The following table describes the properties included in the `tenantCandidates` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| tenantCandidate | tenant Candidate | Specifies a tenant that is ( `tenantCandidates`) or is not ( `tenantConflictingCandidates`) eligible for inclusion in an erasure coding topology. |  |\
\
## Example\
\
Here's an XML example of the `tenantCandidates` data type; the properties shown are those that are returned in response to a verbose GET request:\
\
```\
<tenantCandidates>\
    <tenantCandidate>\
        <hcpSystems>\
            <name>hcp-eu.example.com</name>\
            <name>hcp-us.example.com</name>\
        </hcpSystems>\
        <name>finance</name>\
        <uuid>838cd575-0f94-489a-8f94-f36c1337c446</uuid>\
    </tenantCandidate>\
    <tenantCandidate>\
        <hcpSystems>\
            <name>hcp-us.example.com</name>\
        </hcpSystems>\
        <name>it</name>\
        <uuid>5c881746-5a93-48d0-a55a-d1aaec50c522</uuid>\
    </tenantCandidate>\
</tenantCandidates>\
```\
\
### transition\
\
The `transition` data type describes the `transition` property of the `local` and `remote` data types used to describe the `local` and `remote` properties of the replication link schedule resource.\
\
## transition data type properties\
\
The following table describes the properties included in the `transition` data type.\
\
| Property name | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| performanceLevel | String | Specifies the performance level that applies to the replication link on the local or remote system, as applicable, starting at the day and time specified by the time property. Valid values are:<br>LOW<br> The performance level changes to low at the specified day and time.MEDIUM<br> The performance level changes to medium at the specified day and time.HIGH<br> The performance level changes to high at the specified day and time.CUSTOM<br> The performance level changes to the custom setting at the specified day and time.OFF<br> The local or remote system, as applicable, stops sending data on the link at the specified day and time.<br>These values are not case-sensitive.<br>You cannot set the schedule to OFF for the entire week. To stop replication on the link for the entire week, suspend the link. | This property is required for within occurrence of the transition property. |\
| time | Date | Specifies a day and time at which the performance level for the replication link changes to the level specified by the `performanceLevel` property. Valid values are datetime values in this format:<br>`EE:hh`<br>In this format:<br>- EE is the three-letter day (for example, Mon). This value is case-sensitive.<br>- hh is the hour on a 24-hour clock. For midnight, use 00. | This property is required for within occurrence of the transition property. |\
\
## Example\
\
Here’s an XML example of the `transition` data type:\
\
```\
<transition>\
    <time>Tue:18</time>\
    <performanceLevel>HIGH</performanceLevel>\
</transition>\
```\
\
### updatePasswordRequest\
\
The `updatePasswordRequest` data type describes the request to change a password for a system-level user account or a tenant-level user account.\
\
## Properties\
\
The table below describes the property included in the `updatePasswordRequest` data type.\
\
| Property | Data type | Description |\
| --- | --- | --- |\
| newPassword | String | The new password that you want to set for the system-level user account or tenant-level user account. |\
\
## Example\
\
Here’s an XML example of the `updatePasswordRequest` data type:\
\
```\
<updatePasswordRequest>\
    <newPassword>End321!</newPassword>\
</updatePasswordRequest>\
```\
\
### userAccount (tenant level)\
\
The `userAccount` data type describes the `userAccounts` resource.\
\
When you create a user account, you use a query parameter to specify the password for the account. You use the same query parameter to change the password for a user account.\
\
## Properties\
\
The table below describes the properties included in the `userAccount` data type.\
\
| Property | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| allowNamespace Management | Boolean | Specifies whether the user account has the allow namespace management property. Valid values are:<br>true<br> The user account has the allow namespace property.false<br> The user account does not have the allow namespace management property.<br>On a PUT request, the default is true if the roles property includes ADMINISTRATOR in the same request; otherwise, the default is false.<br>On a POST request, adding ADMINISTRATOR to the roles for the user account automatically enables the allow namespace management property for the account.<br>Users with the allow namespace management property can use the HCP management and S3 compatible APIs to:<br>- Create namespaces<br>- List, view and change the versioning status of, and delete namespaces they own | This property is not valid on a PUT request. It is valid on a POST request only if the user making the request has the administrator role. |\
| description | String | Specifies the description of the user account. This description is optional. The default is no description.<br>To remove a description from an existing user account, specify the description property with no value. | This property is valid on a POST request only if the user making the request has the security role. |\
| enabled | Boolean | Specifies whether the user account is enabled. Valid values are:<br>true<br> The user account is enabled.false<br> The user account is disabled. | This property is required on a PUT request. It is valid on a POST request only if the user making the request has the security role. |\
| forcePasswordChange | Boolean | Specifies whether the password for the user account must be changed the next time the account is used to log into the Tenant Management Console. Valid values are:<br>true<br> The password must be changed.false<br> The password does not need to be changed. | This property is required on a PUT request. It is valid on a POST request and returned by a GET request only if the user making the request has the security role. |\
| fullName | String | Specifies the full name of the user for whom you’re creating the account. This name must be from one through 64 characters long and can contain any valid UTF-8 characters, including white space. | This property is required on a PUT request. It is valid on a POST request only if the user making the request has the security role. |\
| localAuthentication | Boolean | Specifies whether the user account is authenticated locally or by a RADIUS server specified at the HCP system level. Valid values are:<br>true<br> The user account is authenticated locally.false<br> The user account is authenticated by a RADIUS server. | This property is required on a PUT request. It is not valid on a POST request and is returned only by a verbose GET request. |\
| roles | List | Associates zero, one, or more roles with the user account. Valid values for roles are:<br>- ADMINISTRATOR<br>- COMPLIANCE<br>- MONITOR<br>- SECURITY<br>These values are not case sensitive.<br>The default is no roles. | This property is valid on a POST request and returned by a GET request only when the user making the request has the security role.<br>For an existing user account, the set of roles specified in the request body replaces the set of roles currently associated with the user account. To remove all roles, specify an empty set.<br>In XML, the element that identifies each role is `role`. In JSON, the name in the name/value pair that lists the roles is `role`. |\
| userGUID | String | Specifies the system-supplied globally unique user ID for the user account. HCP generates this ID automatically when you create an account. | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request and only when the user making the request has the security role. |\
| userID | Integer | Specifies the system-supplied local user ID for the user account. HCP generates this ID automatically when you create an account.<br>Local user IDs are unique within an HCP system. These IDs are maintained for backward compatibility but are no longer used for user identification. | This property is not valid on a PUT or POST request. It is returned only by a verbose GET request and only when the user making the request has the security role. |\
| username | String | Specifies the username for the user account. Usernames must be from one through 64 characters long and can contain any valid UTF-8 characters, including white space, but cannot start with an opening square bracket (\[).<br>Usernames are not case sensitive.<br>The username for a user account must be unique for the tenant. Different tenants can have user accounts with the same username.<br>You can reuse usernames that are not currently in use. So, for example, if you delete the account for a user and then create a new account for that user, you can give the user the same username as before.<br>**Tip:** Consider using email addresses as user names. This enables users to more easily remember their HCP usernames. It also gives you easy access to email addresses should you need to contact any users. | This property is required on a PUT request. It is valid on a POST request only when the user making the request has the security role. |\
\
## Query parameter for setting user account passwords\
\
You use the password query parameter to specify the password for a new user account and to change the password for an existing user account. The value of this parameter is the password you want.\
\
## Example\
\
Here’s an XML example of the `userAccount` data type:\
\
```\
<userAccount>\
    <allowNamespaceManagement>false</allowNamespaceManagement>\
    <description>Compliance officer.</description>\
    <enabled>true</enabled>\
    <forcePasswordChange>true</forcePasswordChange>\
    <fullName>Morgan White</fullName>\
    <localAuthentication>true</localAuthentication>\
    <roles>\
         <role>MONITOR</role>\
         <role>COMPLIANCE</role>\
    </roles>\
    <userGUID>a8ae69dc-e2e3-44a9-aa64-9c142a38ed5d</userGUID>\
    <userID>517</userID>\
    <username>mwhite</username>\
</userAccount>\
```\
\
### versioningSettings\
\
The `versioningSettings` data type describes the `versioningSettings` resource for namespaces and the `versioningSettings` property of the namespace and namespaceDefaults data types.\
\
## Properties\
\
The table below describes the properties included in the `versioningSettings` data type.\
\
| Property | Data type | Description | Notes |\
| --- | --- | --- | --- |\
| daysOnPrimary | Integer | Specifies the number of days old versions of objects must remain in the namespace before they are pruned. Valid values are integers in the range zero through 36,500 (that is, 100 years). A value of zero means prune immediately.<br>The default is zero. | Deprecated; replaced by the `pruneDays` property.<br>If specified on a PUT or POST request, this property has the same function as the `pruneDays` property. You cannot include both this property and the `pruneDays` property in the same request.<br>This property is not returned by any GET request. |\
| daysOnReplica | Integer |  | Deprecated. The pruneDays property applies to the namespace on all systems on which the namespace exists.<br>This property is ignored on a PUT or POST request and is not returned by any GET request. |\
| enabled | Boolean | Specifies whether versioning is enabled for the namespace. Valid values are:<br>true<br> Versioning is enabled.false<br> Versioning is disabled.<br>The default is false. | This property is required on a PUT request to create a namespace and on a POST request to modify namespace defaults if the request includes the `versioningSettings` property.<br>You cannot enable versioning for a namespace while the CIFS, NFS, WebDAV, or SMTP protocol or appendable objects are enabled. |\
| keepDeletionRecords | Boolean | Specifies whether HCP should keep records of deletion operations (delete, purge, prune, disposition) that occur in the namespace if the namespace has ever had versioning enabled. Valid values are:<br>true<br> Keep records of deletion operations.false<br> Do not keep records of deletion operations.<br>The default is true.<br>The amount of time for which HCP keeps deletion records is determined by the system configuration. | This property is not valid on a POST request to modify namespace defaults and is not returned by any GET request for namespace defaults. |\
| prune | Boolean | Specifies whether version pruning is enabled for the namespace. Valid values are:<br>true<br> Version pruning is enabled for the namespace.false<br> Version pruning is disabled for the namespace.<br>The default is false. | This property is required on a PUT request to create a namespace and on a POST request to modify namespace defaults if the enabled property is set to `true`.<br>You cannot include both this property and the `pruneOnPrimary` property in the same request. |\
| pruneDays | Integer | Specifies the number of days old versions of objects must remain in the namespace before they are pruned. Valid values are integers in the range zero through 36,500 (that is, 100 years). A value of zero means prune immediately.<br>The default is zero. | This property is required on a PUT request to create a namespace and on a POST request to modify namespace defaults if the prune property is set to `true`.<br>You cannot include both this property and the `daysOnPrimary` property in the same request. |\
| pruneOnPrimary | Boolean | Specifies whether version pruning is enabled for the namespace. Valid values are:<br>true<br> Version pruning is enabled for the namespace.false<br> Version pruning is disabled for the namespace.<br>The default is false. | Deprecated; replaced by the prune property.<br>If specified on a PUT or POST request, this property has the same function as the `prune` property. You cannot include both this property and the `prune` property in the same request.<br>This property is not returned by any GET request. |\
| pruneOnReplica | Boolean |  | Deprecated. The prune property applies to the namespace on all systems on which the namespace exists.<br>This property is ignored on a PUT or POST request and is not returned by any GET request. |\
| useDeleteMarkers | Boolean | Specifies whether delete markers are used in the namespace. Valid values are:<br>true<br> Delete marker support is enabled for the namespace.false<br> Delete marker support is disabled for the namespace.<br>The default is false. | Modify namespace defaults if the enabled property is set to `true`. |\
\
## Example\
\
Here’s an XML example of the `versioningSettings` data type:\
\
```\
<versioningSettings>\
   <enabled>true</enabled>\
   <keepDeletionRecords>true</keepDeletionRecords>\
   <prune>true</prune>\
   <pruneDays>10</pruneDays>\
   <useDeleteMarkers>true</useDeleteMarkers>\
</versioningSettings>\
```\
\
## Usage considerations\
\
This section of the Help contains usage considerations for the HCP management API.\
\
### Choosing an access method\
\
You can access the HCP system through the management API by specifying either a hostname or an IP address in the resource URL. If the system uses DNS and you specify a hostname, HCP selects the IP address for you from the currently available nodes. HCP uses a round-robin method to ensure that it doesn’t always select the same address.\
\
When you specify IP addresses, your application must take responsibility for balancing the load among the nodes. In this case, you risk trying to connect (or reconnect) to a node that is not available. However, using explicit IP addresses to connect to specific nodes can sometimes have advantages over using hostnames.\
\
These considerations apply when deciding which method to use:\
\
- If the client uses a hosts file to map HCP hostnames to IP addresses, the client system has full responsibility for converting any hostnames to IP addresses. Therefore, HCP cannot spread the load or prevent attempts to connect to unavailable nodes.\
- If the client caches DNS information, connecting by hostname may result in the same node being used repeatedly.\
- When you access the HCP system by hostname, HCP ensures that requests are distributed among the nodes, but it does not ensure that the resulting loads on the nodes are evenly balanced.\
- When multiple applications access the HCP system by hostname concurrently, HCP is less likely to spread the load evenly across the nodes than with a single application.\
\
Note: When using hostnames, you can ping the HCP system periodically to check whether you’re getting connections to different nodes.\
\
\
### Generating templates for resource creation\
\
When you use the HCP management API to create a resource, the best way to ensure that the request body includes the applicable properties is to use a template. You can generate your own template by submitting a GET request for an existing resource of the same type. In the request, include the `verbose=false` query parameter on the resource URL (or omit the `verbose` parameter to accept the default of `false`).\
\
With a nonverbose GET request HCP returns only properties whose values you can set. This enables you to use the response body as a template for the request body for creating additional resources of the same type.\
\
In most cases, in response to a nonverbose GET request, HCP returns all the properties required for creating a resource of the same type and none of the properties that are invalid on a PUT request. The only exception is for namespaces, where the response body does not include the `versioningSettings` property. To complete the template for namespaces, you need to add this property.\
\
### Modifying resources\
\
1. Submit a GET request for the resource you want to modify.\
\
    In the request, include the `verbose=false` query parameter on the resource URL (or omit the verbose parameter to accept the default of false). This ensures that the response body includes only the properties whose values you can set.\
\
\
2. In the response body from the GET request, change property values as needed.\
3. Submit the POST request for the resource, using the entire modified response body as the request body.\
\
By modifying a resource in this way, you ensure that all the property values are set as expected. A possible drawback to this method is that someone else can modify the resource in between your GET and POST requests. In this case, the values in your POST request will overwrite any modifications. However, this may, in fact, be the result you want.\
\
### Session cookie encoding\
\
In the response to a client request, HCP includes a cookie that contains encoded session information.\
\
HCP supports two formats for encoding the session cookie:\
\
RFC2109\
HCP used only this format in releases 5.0 and earlier.\
RFC6265\
HCP has used this format by default in all releases since 5.0.\
\
\
You can use the `X-HCP-CookieCompatibility` request header to specify the format HCP should use to encode the session cookie. Valid values for this header are RFC2109 and RFC6265.\
\
The `X-HCP-CookieCompatibility` header is:\
\
- Optional and typically not used for RFC6265\
- Required for RFC2109\
\
## HTTP status codes for the HCP management API\
\
This table below explains the possible status codes for HCP management API requests.\
\
| Code | Meaning | Methods | Description |\
| --- | --- | --- | --- |\
| 200 | OK | All | HCP successfully created, retrieved, checked the existence of, modified, or deleted a resource. |\
| 302 | Found | HEAD | Either the specified resource does not exist, or the specified resource exists, but the user account identified by the Authorization header or `hcp-api-auth` cookie doesn’t have permission to access the resource. |\
| 400 | Bad Request | All | The request was not valid. These are some, but not all, of the possible reasons:<br>- The URL in the request is not well-formed.<br>- The request is missing a required query parameter.<br>- The request contains a required or optional non-Boolean query parameter with an invalid value.<br>- For a PUT or POST request, the request body:<br>  - Is missing a required property<br>  - Includes a property that is invalid for the resource<br>  - Has a property with an invalid value<br>  - Contains XML or JSON that is not well-formed<br>- The `Content-Type` or `Accept` header in the request specifies a nonexistent Internet media type.<br>If more information about the error is available, the response headers include the HCP-specific `X‑HCP-ErrorMessage` header. |\
| 401 | Unauthorized | All | HCP was unable to handle the request. If this happens repeatedly, contact your authorized HCP service provider for help. |\
| 403 | Forbidden | All | The requested operation is not allowed. These are some, but not all, of the possible reasons:<br>- The URL in the request is missing the port number (9090).<br>- The request does not include an `Authorization` header or `hcp-ns-auth` cookie.<br>- The `Authorization` header or `hcp-ns-auth` cookie specifies invalid credentials.<br>- The user account identified by the `Authorization` header or `hcp-api-auth` cookie is either a system-level account where a tenant-level account is required or a tenant-level account where a system-level account is required.<br>- The user account identified by the `Authorization` header or `hcp-api-auth` cookie doesn’t have permission to perform the requested operation.<br>- For a `PUT` or `POST` request, the request body includes a property that is valid for the resource but that cannot be modified by the requested operation.<br>- For a `DELETE` request:<br>   <br>  - For a namespace, HCP could not delete the namespace because it contains one or more objects.<br>  - For a user account, HCP could not delete the user account because it is the last locally authenticated, enabled user account with the security role, and no group accounts have the security role.<br>- For a tenant-level request, the HCP management API is not enabled for the tenant. Use the Tenant Management Console for the applicable tenant to enable the API.<br>If more information about the error is available, the response headers include the HCP-specific `X‑HCP-ErrorMessage` header. |\
| 404 | Not Found | All | The resource identified by the URL does not exist. |\
| 405 | Method Not Allowed | PUT<br>POST<br>DELETE | The requested operation is not valid for the resource identified by the URL. |\
| 409 | Conflict | PUT | For a PUT request, HCP could not create the resource because it already exists.<br>If more information about the error is available, the response headers include the HCP-specific `X‑HCP-ErrorMessage` header. |\
| 414 | Request URI Too Large | All | The portion of the URL following `rest` is longer than 4,095 bytes. |\
| 415 | Unsupported Media Type | All | Either the management API does not support the Internet media type specified by the `Content-Type` or `Accept` header, or the request includes a request body but no `Content-Type` header. |\
| 500 | Internal Server Error | All | An internal error occurred. If this happens repeatedly, contact your authorized HCP service provider for help. |\
| 503 | Service Unavailable | All | HCP is temporarily unable to handle the request. Possible reasons include:<br>- HCP is currently unavailable due to system overload, maintenance, or upgrade. Try the request again in a little while.<br>- The HCP system is currently being upgraded.<br>If more information about the error is available, the response headers include the HCP-specific `X‑HCP-ErrorMessage` header. |\
\
## Sample Java application\
\
This section of the Help contains a sample Java® application that uses the HCP management API to define various resources in an HCP system. It also shows the content of the JSON files that serve as input to the application.\
\
### What the application does\
\
The sample Java application shown in this appendix uses the HCP management API to:\
\
01. Give the initial tenant-level user account the administrator role\
02. Create a tenant-level user account with the monitor and compliance roles\
03. Configure the Tenant Management Console\
04. Enable syslog logging for the tenant and system-level administrative access to the tenant\
05. Create two namespaces\
06. Enable disposition for one of the namespaces\
07. Configure the REST API for both namespaces\
08. Create a tenant-level user account with no roles\
09. Grant data access permissions to the user account with no roles\
10. Create one retention class\
\
### Assumptions\
\
The sample Java application assumes:\
\
- That the system against which you’re running the application is named hcp-ma.example.com\
- The existence of a system-level HCP user account with the username `rsilver` and the password `p4ssw0rd`\
- That all input files are located in the /home/rsilver/MAPI directory\
- The existence of a service plan named Short-Term-Activity\
- That HCP is configured to support Active Directory\
- The existence of another HCP system named hcp-ca.example.com\
- That the system against which you’re running the application and the system named hcp-ca.example.com have exchanged replication SSL server certificates with each other\
\
If you want to run the sample application, you may need to modify the sample input JSON files to conform to your HCP system.\
\
### Required libraries\
\
To run the sample application presented in this appendix, you need to have these Java libraries installed:\
\
- HttpClient 4.0 (httpclient-4.0.jar)\
- HttpCore 4.0 (httpcore-4.0.1.jar)\
- Commons Logging 1.1 (commons-logging-1.1.jar)\
\
You can download the first two of these libraries from: [http://hc.apache.org/downloads.cgi](http://hc.apache.org/downloads.cgi)\
\
You can download the third one from: [http://commons.apache.org/logging/download\_logging.cgi](http://commons.apache.org/logging/download_logging.cgi)\
\
### Input JSON files\
\
The sample Java application uses the JSON files shown in the following sections.\
\
#### JSON file for creating the HCP tenant\
\
Here’s the content of the JSON file that creates an HCP tenant named Finance. The name of the file is FinanceTenant.json. For the username and password of the initial user account for this tenant, the sample application specifies lgreen and start123, respectively.\
\
```\
{\
 "name" : "Finance",\
  "systemVisibleDescription" : "Created for the Finance department at Example Company by Robin Silver",\
  "hardQuota" : "100.00 GB",\
  "softQuota" : 90,\
  "namespaceQuota" : "5",\
  "authenticationTypes" : {\
    "authenticationType" : ["EXTERNAL", "LOCAL"]\
 },\
"complianceConfigurationEnabled" : true,\
"versioningConfigurationEnabled" : true,\
"searchConfigurationEnabled" : true,\
"replicationConfigurationEnabled" : true,\
"tags" : {\
   "tag" : [ "Example Company", "pdgrey" ]\
 },\
 "servicePlanSelectionEnabled" : false,\
 "servicePlan" : "Short-Term-Activity"\
}\
```\
\
#### JSON file for modifying the initial user account\
\
Here’s the content of the JSON file that adds the administrator role to the lgreen user account. The name of the file is lgreen-UAroles.json.\
\
```\
{\
  "roles" : {\
    "role" : [ "SECURITY", "ADMINISTRATOR" ]\
   }\
}\
```\
\
#### JSON file for creating the user account with the compliance and monitor roles\
\
Here’s the content of the JSON file that creates a user account with the monitor and compliance roles. The name of the file is mwhite-UA.json. For the username and password for this account, the sample application specifies `mwhite` and `start123`, respectively.\
\
```\
{\
  "username" : "mwhite",\
  "fullName" : "Morgan White",\
  "description" : "Compliance officer.",\
  "localAuthentication" : true,\
  "forcePasswordChange" : false,\
  "enabled" : true,\
  "roles" : {\
      "role" : [ "COMPLIANCE", "MONITOR" ]\
  }\
}\
```\
\
#### JSON file for configuring the Tenant Management Console\
\
Here’s the content of the JSON file that configures the Tenant Management Console for the Finance tenant. The name of the file is FinanceMgmtConsole.json.\
\
```\
{\
  "ipSettings" : {\
     "allowAddresses" : {\
       "ipAddress" : [ "192.168.103.18", "192.168.103.24", "192.168.103.25" ]\
     },\
     "denyAddresses" : {\
       "ipAddress" : [ ]\
     },\
    "allowIfInBothLists" : false\
  },\
  "minimumPasswordLength" : 6,\
   "forcePasswordChangeDays" : 45,\
   "disableAfterAttempts" : 3,\
   "disableAfterInactiveDays" : 30,\
   "logoutOnInactive" : 20,\
   "loginMessage" : "Welcome to the Finance Tenant"\
}\
```\
\
#### JSON file for modifying the tenant\
\
Here’s the content of the JSON file that enables syslog logging for the Finance tenant and also enables system-level administrative access to the tenant. The name of the file is modFinance.json.\
\
```\
{\
   "administrationAllowed" : true,\
   "maxNamespacesPerUser" : 5,\
   "syslogloggingEnabled" : true\
}\
```\
\
#### JSON files for creating the namespaces\
\
Here’s the content of the JSON file that creates a namespace named Accounts-Receivable. The name of the file is AccountsRecNS.json.\
\
```\
{\
    "name" : "Accounts-Receivable",\
    "description" : "Created for the Finance department at Example Company by Lee Green",\
    "hashScheme" : "SHA-256",\
    "enterpriseMode" : true,\
    "hardQuota" : "50.00 GB",\
    "softQuota" : 75,\
    "versioningSettings" : {\
    "enabled" : true,\
    "prune" : true,\
    "pruneDays" : 10,\
    "useDeleteMarkers" : true,\
    },\
    "aclsUsage" : "ENABLED",\
    "searchEnabled" : true,\
    "indexingEnabled" : true,\
    "customMetadataIndexingEnabled" : true,\
    "replicationEnabled" : true,\
    "readFromReplica" : true,\
    "serviceRemoteSystemRequests" : true,\
    "tags" : {\
    "tag" : [ "Billing", "lgreen" ]\
    }\
}\
```\
\
Here’s the content of the JSON file that creates a namespace named Accounts-Payable. The name of the file is AccountsPayNS.json.\
\
```\
{\
    "name" : "Accounts-Payable",\
    "description" : "Created for the Finance department at Example Company by Lee Green",\
    "hashScheme" : "SHA-256",\
    "enterpriseMode" : true,\
    "hardQuota" : "50.00 GB",\
    "softQuota" : 75,\
    "versioningSettings" : {\
      "enabled" : true,\
        "prune" : true,\
        "pruneDays" : 10,\
        "useDeleteMarkers" : true,\
    },\
    "aclsUsage" : "ENABLED",\
    "searchEnabled" : true,\
    "indexingEnabled" : true,\
    "customMetadataIndexingEnabled" : true,\
    "replicationEnabled" : true,\
    "readFromReplica" : true,\
    "serviceRemoteSystemRequests" : true,\
    "tags" : {\
        "tag" : [ "Billing", "lgreen" ]\
    }\
}\
```\
\
#### JSON file for modifying a namespace\
\
Here’s the content of the JSON file that enables disposition for the Accounts-Receivable namespace. The name of the file is AcctsRecCompliance.json.\
\
```\
{\
   "dispositionEnabled" : true\
}\
```\
\
#### JSON file for configuring the REST API\
\
Here’s the content of the JSON file that is used to configure the REST API for both namespaces. The name of the file is http.json.\
\
```\
{\
   "httpsEnabled" : true,\
   "httpEnabled" : false,\
   "restEnabled" : "true",\
   "restRequiresAuthentication" : true,\
   "httpActiveDirectorySSOEnabled" : true,\
   "ipSettings" : {\
   "allowAddresses" : {\
    "ipAddress" : [ "192.168.140.10", "192.168.140.15",     192.168.149.0/24" ]\
   },\
     "denyAddresses" : {\
       "ipAddress" : [ "192.168.149.5" ]\
     },\
     "allowIfInBothLists" : false\
   }\
}\
```\
\
#### JSON file for creating the user account with no roles\
\
Here’s the content of the JSON file that creates a user account with the username `pblack`. The name of the file is pblack-UA.json. For the password for this account, the sample application specifies `start123`.\
\
```\
{\
   "username" : "pblack",\
   "fullName" : "Paris Black",\
   "description" : "Data access user.",\
   "localAuthentication" : true,\
   "forcePasswordChange" : false,\
   "enabled" : true\
}\
```\
\
#### JSON file for granting data access permissions to the user account\
\
Here’s the content of the JSON file that grants data access permissions to the user account with the username `pblack`. The name of the file is pblack-UAperms.json.\
\
```\
{\
   "namespacePermission" : [ {\
     "namespaceName" : "Accounts-Receivable",\
     "permissions" : {\
       "permission" : [ "BROWSE", "READ", "SEARCH", "PURGE", "DELETE", "WRITE" ]\
     }\
   }, {\
     "namespaceName" : "Accounts-Payable",\
     "permissions" : {\
       "permission" : [ "BROWSE", "READ" ]\
     }\
   } ]\
}\
```\
\
#### JSON file for creating the retention class\
\
Here’s the content of the JSON file that creates a retention class named Fn-Std-42 for the Accounts-Receivable namespace. The name of the file is RC-FN-Std-42.json.\
\
```\
{\
   "name" : "FN-Std-42",\
   "description" : "Implements Finance department standard #42 - keep for 10 years.",\
   "value" : "A+10y",\
   "allowDisposition" : true\
}\
```\
\
#### JSON file for creating the replication link\
\
Here’s the content of the JSON file that creates a replication link named MA-CA. The name of the file is LinkMA-CA.json.\
\
```\
{\
   "name" : "MA-CA",\
   "description" : "Active/active link between systems in MA and CA",\
   "type" : "ACTIVE_ACTIVE",\
   "connection" : {\
     "remoteHost" : "replication.admin.hcp-ca.example.com"\
   },\
   "compression" : false,\
   "encryption" : false,\
   "priority" : "OLDEST_FIRST",\
   "failoverSettings" : {\
     "remote" : {\
       "autoFailover" : true,\
       "autoFailoverMinutes" : 60\
   },\
     "local" : {\
       "autoFailover" : true,\
       "autoFailoverMinutes" : 60\
     }\
   }\
}\
```\
\
### JAVA application\
\
Here is the sample Java application that uses the JSON files shown in the preceding sections.\
\
```\
import sun.misc.BASE64Encoder;\
\
import java.security.MessageDigest;\
import java.security.NoSuchAlgorithmException;\
import java.security.KeyManagementException;\
import java.io.*;\
import java.net.URI;\
import java.net.URISyntaxException;\
import java.util.List;\
import java.util.ArrayList;\
\
import org.apache.http.conn.scheme.SchemeRegistry;\
import org.apache.http.conn.scheme.Scheme;\
import org.apache.http.conn.ssl.SSLSocketFactory;\
import org.apache.http.conn.ClientConnectionManager;\
import org.apache.http.conn.params.ConnPerRouteBean;\
import org.apache.http.conn.params.ConnManagerParams;\
import org.apache.http.params.HttpParams;\
import org.apache.http.params.BasicHttpParams;\
import org.apache.http.impl.conn.tsccm.ThreadSafeClientConnManager;\
import org.apache.http.impl.client.DefaultHttpClient;\
import org.apache.http.impl.client.AbstractHttpClient;\
import org.apache.http.HttpHost;\
import org.apache.http.HttpResponse;\
import org.apache.http.NameValuePair;\
import org.apache.http.HttpRequest;\
import org.apache.http.message.BasicNameValuePair;\
import org.apache.http.entity.FileEntity;\
import org.apache.http.client.utils.URIUtils;\
import org.apache.http.client.utils.URLEncodedUtils;\
import org.apache.http.client.methods.HttpPut;\
import org.apache.http.client.methods.HttpPost;\
import org.apache.http.util.EntityUtils;\
\
import javax.net.ssl.SSLContext;\
import javax.net.ssl.TrustManager;\
import javax.net.ssl.TrustManagerFactory\
import javax.net.ssl.X509TrustManager\
import java.security.cert.X509Certificate\
import java.security.cert.CertificateException\
import java.security.cert.KeyStore\
import java.security.cert.KeyStoreException\
\
/**\
* HCP Management API - Sample Java Application\
*/\
public class MAPISample {\
\
    private AbstractHttpClient httpClient = null;\
    private String protocol;\
    private int port;\
    private String uname64;\
    private String encodedPassword;\
\
    private String hcpSystemAddr;\
\
    private enum RequestType {\
        PUT, POST;\
    }\
\
    public class HCPNotInitializedException extends Exception {\
        public HCPNotInitializedException(String msg) {\
            super("HTTP client could not be initialized in HCPAdapter for the " +\
                "following reason: " + msg);\
        }\
    }\
\
    public static void main(String [] args) {\
        MAPISample adapter = null;\
        try {\
            adapter = new MAPISample();\
\
            // Switch the adapter to the initial user account for the new tenant.\
            adapter.setUpSystemInfo(hcpSystemAddr, tenantUser, tenantPassword);\
\
            // Modify the initial user account, using lgreen-UAroles.json as input.\
            f = new File("/home/rsilver/MAPI/lgreen-UAroles.json");\
            adapter.modifylUserAccount(tenantName, tenantUser, f);\
\
            // Create a user account for compliance, using mwhite-UA.json as input\
            // and specifying start123 as the account password.\
            f = new File("/home/rsilver/MAPI/mwhite-UA.json");\
            adapter.createTenantUserAccount(tenantName, "start123", f);\
\
            // Configure the Tenant Management Console, using FinanceMgmtConsole.json\
            // as input.\
            f = new File("/home/rsilver/MAPI/FinanceMgmtConsole.json");\
            adapter.configureTenantSecurity(tenantName, f);\
\
            // Modify the tenant, using modFinance.json as input.\
            f = new File("/home/rsilver/MAPI/modFinance.json");\
            adapter.modifyTenant(tenantName, f);\
\
            // Create a namespace, using AccountsRecNS.json as input.\
            f = new File("/home/rsilver/MAPI/AccountsRecNS.json");\
            adapter.createNamespace(tenantName, f);\
\
            // Create a namespace, using AccountsPayNS.json as input.\
            f = new File("/home/rsilver/MAPI/AccountsPayNS.json");\
            adapter.createNamespace(tenantName, f);\
\
            // Modify the Accounts-Receivable namespace, using\
            // AcctsRecCompliance.json as input.\
            String namespaceName = "Accounts-Receivable";\
            f = new File("/home/rsilver/MAPI/AcctsRecCompliance.json");\
            adapter.modifyNamespace(namespaceName, tenantName, f);\
\
            // Configure the REST API for the Accounts-Receivable namespace, using http.json\
            // as input.\
            String namespaceName = "Accounts-Receivable";\
            f = new File("/home/rsilver/MAPI/http.json");\
            adapter.modifyNamespaceHTTP(namespaceName, tenantName, f);\
\
            // Configure the REST API for the Accounts-Payable namespace, using http.json\
            // as input.\
            String namespaceName = "Accounts-Payable";\
            f = new File("/home/rsilver/MAPI/http.json");\
           adapter.modifyNamespaceHTTP(namespaceName, tenantName, f);\
\
            // Create a user account with no roles, using pblack-UA.json as input\
            // and specifying start123 as the account password.\
            f = new File("/home/rsilver/MAPI/pblack-UA.json");\
            adapter.createTenantUserAccount(tenantName, "start123", f);\
\
            // Modify the user account, using pblack-UAperms.json as input.\
            String userAcctName = "pblack"\
            f = new File("/home/rsilver/MAPI/pblack-UAperms.json");\
            adapter.changeDataUserAccountPerms(userAcctName, tenantName, f);\
\
           // Create a retention class, using RC-FN-Std-42.json as input.\
            f = new File("/home/rsilver/MAPI/RC-FN-Std-42.json");\
            adapter.createRetentionClass(namespaceName, tenantName, f);\
\
        } catch (HCPNotInitializedException e) {\
            e.printStackTrace();\
        } finally {\
            if(adapter != null) {\
                adapter.shutdownHttpClient();\
            }\
        }\
    }\
\
    /**\
     * Constructor - initializes the HTTP client.\
     */\
    public MAPISample() throws HCPNotInitializedException{\
        initHttpClient();\
    }\
\
    /**\
     * When done with this adapter, shut it down.\
     */\
    public void shutdownHttpClient() {\
        httpClient.getConnectionManager().shutdown();\
    }\
\
    /**\
     * Initialize the HCP system access settings.\
     * @param hcpSystemAddr\
     * @param username\
     * @param password\
     */\
    public void setUpSystemInfo(String hcpSystemAddr, String username,\
                                String password)\
    {\
        // This is the root for management API commands. In general, these values\
        // should be retrieved from configuration settings.\
        this.hcpSystemAddr = hcpSystemAddr;\
\
        // The management API requires HTTPS and port 9090.\
        protocol = "https";\
        port = 9090;\
\
        // Calculate the authentication token for management API access to HCP.\
        BASE64Encoder base64Encoder = new BASE64Encoder();\
        uname64 = base64Encoder.encode(username.getBytes());\
        encodedPassword = toMD5Digest(password);\
    }\
\
    public void modifylUserAccount(String tenantName, String username,\
                                   File jsonInputFile) {\
        String addr = tenantName + "." + hcpSystemAddr;\
        String path = "/mapi" + "/tenants/" + tenantName +\
                      "/userAccounts/" + username;\
\
        this.executeRequest(RequestType.POST, addr, path, null, jsonInputFile);\
   }\
\
    public void createTenantUserAccount(String tenantName, String password,\
                                        File jsonInputFile) {\
        String addr = tenantName + "." + hcpSystemAddr;\
        String path = "/mapi" + "/tenants/" + tenantName + "/userAccounts";\
\
        List<NameValuePair> metadata = new ArrayList<NameValuePair>();\
        metadata.add(new BasicNameValuePair("password", password));\
        String queryString = URLEncodedUtils.format(metadata, "UTF-8");\
\
        this.executeRequest(RequestType.PUT, addr, path, queryString, jsonInputFile);\
    }\
\
    public void configureTenantSecurity(String tenantName, File jsonInputFile) {\
        String addr = tenantName + "." + hcpSystemAddr;\
        String path = "/mapi" + "/tenants/" + tenantName + "/consoleSecurity";\
\
        this.executeRequest(RequestType.POST, addr, path, null, jsonInputFile);\
    }\
\
    public void modifyTenant(String tenantName, File jsonInputFile) {\
        String addr = tenantName + "." + hcpSystemAddr;\
        String path = "/mapi" + "/tenants/" + tenantName;\
\
        this.executeRequest(RequestType.POST, addr, path, null, jsonInputFile);\
    }\
\
    public void createNamespace(String tenantName, File jsonInputFile) {\
        String addr = tenantName + "." + hcpSystemAddr;\
        String path = "/mapi" + "/tenants/" + tenantName + "/namespaces";\
\
        this.executeRequest(RequestType.PUT, addr, path, null, jsonInputFile);\
    }\
\
    public void modifyNamespace(String namespaceName, String tenantName,\
                                File jsonInputFile) {\
        String addr = tenantName + "." + hcpSystemAddr;\
        String path = "/mapi" + "/tenants/" + tenantName + "/namespaces/" +\
                      namespaceName + "/complianceSettings";\
\
        this.executeRequest(RequestType.POST, addr, path, null, jsonInputFile);\
    }\
\
    public void modifyNamespaceHTTP(String namespaceName, String tenantName,\
                                File jsonInputFile) {\
        String addr = tenantName + "." + hcpSystemAddr;\
        String path = "/mapi" + "/tenants/" + tenantName + "/namespaces/" +\
                      namespaceName + "/protocols/http";\
\
        this.executeRequest(RequestType.POST, addr, path, null, jsonInputFile);\
    }\
\
    public void changeDataUserAccountPerms(String dataUserAcctName,\
                                             String tenantName, File jsonInputFile) {\
        String addr = tenantName + "." + hcpSystemAddr;\
        String path = "/mapi" + "/tenants/" + tenantName + "/userAccounts/" +\
                      dataUserAcctName + "/dataAccessPermissions";\
\
        List<NameValuePair> metadata = new ArrayList<NameValuePair>();\
        metadata.add(new BasicNameValuePair("debug", "true"));\
        String queryString = URLEncodedUtils.format(metadata, "UTF-8");\
\
        this.executeRequest(RequestType.POST, addr, path, queryString,\
                            jsonInputFile);\
    }\
\
    public void createRetentionClass(String namespaceName, String tenantName,\
                                     File jsonInputFile) {\
        String addr = tenantName + "." + hcpSystemAddr;\
        String path = "/mapi" + "/tenants/" + tenantName + "/namespaces/" +\
                      namespaceName + "/retentionClasses";\
\
        this.executeRequest(RequestType.PUT, addr, path, null, jsonInputFile);\
    }\
\
    /**\
     * Execute the HTTP request to perform the applicable management API operation.\
     * @param requestType\
     * @param addr\
     * @param path\
     * @param queryString\
     * @param jsonInputFile\
     */\
    private void executeRequest(RequestType requestType, String addr, String path,\
                                String queryString, File jsonInputFile) {\
\
        boolean success = false;\
        try {\
            // Set up the HTTP host.\
            HttpHost httpHost = new HttpHost(addr, port, protocol);\
\
            URI uri = URIUtils.createURI(protocol, addr, port, path, queryString,\
                                         null);\
\
            // JSON file.\
            FileEntity fileEntity = new FileEntity(\
                jsonInputFile, "application/json; charset=\"UTF-8\"");\
\
            HttpRequest request;\
            if(requestType == RequestType.PUT) {\
                request = new HttpPut(uri);\
                ((HttpPut)request).setEntity(fileEntity);\
            } else {\
                request = new HttpPost(uri);\
                ((HttpPost)request).setEntity(fileEntity);\
            }\
\
            // Set up the authentication header.\
            String header = "HCP " + uname64 + ":" + encodedPassword;\
            request.setHeader("Authorization", header);\
\
            // You should retry the request if the execute throws an IOException or\
            // if HCP returns a server error. You should put the number of retry\
            // attempts in a configuration file that can be changed depending on\
            // network conditions.\
            int retries = 3;\
            while(retries > 0)\
            {\
                --retries;\
                HttpResponse response = null;\
                try {\
                    response = httpClient.execute(httpHost, request);\
                    if (response != null)\
                    {\
                        // Get back the status and log it.\
                        int statusCode = response.getStatusLine().getStatusCode();\
                        System.out.println("Status code for PUT = " + statusCode);\
\
                        // PUT returns a 201 (Created) if it is successful.\
                        if(statusCode == 201) {\
                            success = true;\
                        }\
\
                        // Status codes below 500 are due to either a successful\
                        // PUT, an error by the client, or an authentication error.\
                        // Errors >= 500 are HCP server errors, so you should retry\
                        // on those errors.\
                        if(statusCode < 500) {\
                            retries = 0;\
                            // Notify the user about the error. For descriptions of\
                            // the management API status codes, see Appendix A.\
                        }\
                        else {\
                            if(retries == 0) {\
                                // Notify your HCP system administrator about the\
                                // error.\
                             }\
\
                             // Wait two minutes; then try the request again.\
                             Thread.sleep(2*60*1000);\
                         }\
                    }\
\
                }\
                catch(IOException e) {\
                    // An IOException from the client means there was a transport\
                    // error and is likely a one-off I/O issue. Try the request\
                    // again.\
                    e.printStackTrace();\
\
                    if(retries == 0) {\
                        // Notify your network administrator.\
                    }\
                }\
                // Clean up after ourselves and release the HTTP connection to the\
                // connection manager.EntityUtils.consume\
                        (httpResponse.getEntity());\
            }\
        } catch (URISyntaxException e) {\
            e.printStackTrace();\
        } catch(InterruptedException e) {\
            e.printStackTrace(); // Wait.\
        }\
    }\
\
    /**\
     * Start the HTTP client.\
     */\
    private void initHttpClient() throws HCPNotInitializedException\
    {\
        // Register the HTTPS scheme.\
        SchemeRegistry schemeRegistry = new SchemeRegistry();\
        try {\
            // The recommended protocol is TLS.\
            SSLContext sslcontext = SSLContext.getInstance("TLS");\
\
            // The trust manager used here was created for use with this sample\
            // application. For more information about creating trust managers, see\
            // http://java.sun.com/j2se/1.5.0/docs/guide/security/jsse/\
            // JSSERefGuide.html#TrustManager\
            MyX509TrustManager trustMgr = new MyX509TrustManager();\
\
            sslcontext.init(null, new TrustManager[] {trustMgr}, null);\
            SSLSocketFactory sslSocketFactory = new SSLSocketFactory(sslcontext);\
\
            // The hostname verifier verifies that the hostname matches the one\
            // stored in the X.509 certificate on the server (that is, the SSL\
            // server certificate used by the HCP system). You can use\
            // AllowAllHostnameVerifier, BrowserCompatHostnameVerifier, or\
            // StrictHostnameVerifier. This sample application allows all hostnames.\
            sslSocketFactory.setHostnameVerifier(\
                SSLSocketFactory.ALLOW_ALL_HOSTNAME_VERIFIER);\
\
            // Register the HTTPS scheme.\
            Scheme https = new Scheme("https", sslSocketFactory, 9090);\
            schemeRegistry.register(https);\
\
            // Specify any HTTP parameters you want.\
            HttpParams params = new BasicHttpParams();\
            params.setIntParameter("http.connection.timeout", 60000);\
\
            // This manages a thread-safe pool of connections that are created on\
            // first request, then persisted and leased out to subsequent requests.\
            // By default, HCP closes a connection after ten minutes. To change\
            // this setting, contact your authorized HCP service provider.\
            ClientConnectionManager connMgr = new ThreadSafeClientConnManager(\
                params, schemeRegistry);\
            ConnPerRouteBean connPerRoute = new ConnPerRouteBean(20);\
\
            // HCP recommended settings: max connections per node = 20;\
            // total max connections = 200\
            ConnManagerParams.setMaxConnectionsPerRoute(params, connPerRoute);\
            ConnManagerParams.setMaxTotalConnections(params, 200);\
\
            // Ensure that the connection manager does not block indefinitely in the\
            // connection request operation.\
            ConnManagerParams.setTimeout(params, 2000); // milleseconds\
\
            // Create the HTTP client.\
            httpClient = new DefaultHttpClient(connMgr, params);\
\
        } catch (NoSuchAlgorithmException e1) {\
            throw new HCPNotInitializedException(e1.getMessage());\
        } catch (KeyManagementException e1) {\
            throw new HCPNotInitializedException(e1.getMessage());\
        }\
    }\
\
    private static final String HEX_DIGITS[] = {"0", "1", "2", "3", "4", "5", "6",\
                                                "7", "8", "9", "A", "B", "C", "D",\
                                                "E", "F"};\
    private static String encodeBytes(byte[] bytes) {\
        if (bytes == null || bytes.length == 0) {\
            return "";\
        }\
\
        StringBuffer out = new StringBuffer(bytes.length * 2);\
\
        byte ch;\
        for (int i = 0; i < bytes.length; i++) {\
            ch = (byte) (bytes[i] & 0xF0);\
            ch = (byte) (ch >>> 4);\
            ch = (byte) (ch & 0x0F);\
            out.append(HEX_DIGITS[(int) ch]);\
            ch = (byte) (bytes[i] & 0x0F);\
            out.append(HEX_DIGITS[(int) ch]);\
        }\
\
        return out.toString();\
    }\
\
    protected String toMD5Digest(String sInStr) {\
        StringBuffer mOutDigest = new StringBuffer("");\
\
        try {\
            MessageDigest pMD = MessageDigest.getInstance("MD5");\
\
            byte pDigest[] = pMD.digest(sInStr.getBytes());\
\
            // Convert to string.\
            for(int i=0; i < pDigest.length; i++) {\
                mOutDigest.append(Integer.toHexString(0xFF & pDigest[i]));\
            }\
        }\
        catch (Exception e) {\
            System.err.println("Error: " + e.getMessage());\
            e.printStackTrace();\
        }\
\
        return mOutDigest.toString();\
    }\
\
}\
\
/* Simple trust manager implementation. */\
\
class MyX509TrustManager implements X509TrustManager {\
    private X509TrustManager standardTrustManager = null;\
\
    public MyX509TrustManager() {\
   }\
\
    public MyX509TrustManager(KeyStore keystore)\
              throws NoSuchAlgorithmException, KeyStoreException {\
        super();\
        TrustManagerFactory factory =\
            TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());\
        factory.init(keystore);\
        TrustManager[] trustmanagers = factory.getTrustManagers();\
        if (trustmanagers.length == 0) {\
            throw new NoSuchAlgorithmException("no trust manager found");\
        }\
        this.standardTrustManager = (X509TrustManager)trustmanagers[0];\
    }\
\
    public void checkClientTrusted(X509Certificate[]\
        certificates,String authType) throws CertificateException {\
            standardTrustManager.checkClientTrusted(certificates,authType);\
    }\
\
    public void checkServerTrusted(X509Certificate[]\
        certificates,String authType) throws CertificateException {\
            if ((certificates != null) && (certificates.length == 1)) {\
                certificates[0].checkValidity();\
            } else {\
                standardTrustManager.checkServerTrusted(certificates,authType);\
            }\
    }\
\
    public X509Certificate[] getAcceptedIssuers() {\
        return this.standardTrustManager.getAcceptedIssuers();\
    }\
\
}\
```\
\
## Management API XML schema\
\
HCP uses an XML schema to validate the XML in management API request bodies and to generate the XML in management API response bodies. To retrieve this schema from the HCP system, you use a URL with this format:\
\
```\
https://(tenant-name).hcp-domain-name:9090/static/mapi-9_3_0.xsd.xsd\
```\
\
To retrieve the schema, you need a tenant-level user account with the administrator role.\
\
Here’s a sample curl command that retrieves the management API schema and writes it to a file named mapi\_schema.xsd:\
\
```\
curl -k -i -H "Accept: application/xml"\
    -H "Authorization: HCP YWxscm9sZXM=:04EC9F614D89FF5C7126D32ACB448382"\
    "https://admin.hcp.example.com:9090/static/mapi-9_3_0.xsd"\
    > mapi_schema.xsd\
```
