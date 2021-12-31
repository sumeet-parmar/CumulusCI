CREATE TABLE "Account" (
	id INTEGER NOT NULL, 
	"Name" VARCHAR(255), 
	"Extid__c" VARCHAR(255), 
	PRIMARY KEY (id)
);
CREATE TABLE "Contact" (
	id INTEGER NOT NULL, 
	"FirstName" VARCHAR(255), 
	"LastName" VARCHAR(255), 
	"Extid__c" VARCHAR(255), 
	PRIMARY KEY (id)
);
CREATE TABLE "Opportunity" (
	id INTEGER NOT NULL,
	"Name" VARCHAR(255),
	"CloseDate" VARCHAR(255),
	"Amount" VARCHAR(255),
	"StageName" VARCHAR(255),
	"AccountId" VARCHAR(255),
	PRIMARY KEY (id)
);

INSERT INTO "Opportunity" VALUES (1,'represent Opportunity','2022-01-01','136','In Progress','2');
INSERT INTO "Opportunity" VALUES (3,'another Opportunity','2022-01-01','192','Closed Won','2');
