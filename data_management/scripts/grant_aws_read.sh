#!/bin/bash

## This script is a wrapper around AWS CLI for reading (downloading) data from the S3 bucket.

if [ $# -lt 1 ]; then
  echo "Usage: $0 <S3_PATH> [S3_BUCKET]"
  exit 1
fi

s3_path=$1
s3_bucket=$2

# Build and run aws command to share the folder
cmd="aws sts get-federation-token --name ro_access_to_czbiohub_s3 --duration-seconds 129600 --policy '{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {
     \"Sid\": \"AllowRootListingOfBCLBucket\",
     \"Action\": [\"s3:ListBucket\"],
     \"Effect\": \"Allow\",
     \"Resource\": [\"arn:aws:s3:::$s3_bucket\"],
     \"Condition\":{\"StringEquals\":{\"s3:prefix\":[\"\"],\"s3:delimiter\":[\"/\"]}}
    },
   {
     \"Sid\": \"AllowListingOfSpecificFolder\",
     \"Action\": [\"s3:ListBucket\"],
     \"Effect\": \"Allow\",
     \"Resource\": [\"arn:aws:s3:::$s3_bucket\"],
     \"Condition\":{\"StringLike\":{\"s3:prefix\":[\"$s3_path*\"]}}
   },
   {
     \"Sid\": \"AllowAllS3ActionsInUserFolder\",
     \"Effect\": \"Allow\",
     \"Action\": [\"s3:Get*\"],
     \"Resource\": [\"arn:aws:s3:::$s3_bucket/$s3_path*\"]
   }
 ]
}'"


if [ "$verbose" = 1 ]
then
  echo $cmd
fi
output=$(bash <<EOF
$cmd
EOF
)

ACCESSKEY=$(echo "$output" | grep AccessKeyId | awk '{print $2}' | sed 's/"//g' | sed 's/,$//')
SECRETKEY=$(echo "$output" | grep SecretAccessKey | awk '{print $2}' | sed 's/"//g' | sed 's/,$//')
TOKEN=$(echo "$output" | grep SessionToken | awk '{print $2}' | sed 's/"//g' | sed 's/,$//')

echo \# INSTRUCTIONS FOR DOWNLOADING YOUR FILES
echo \# Ensure that the aws cli is installed: http://docs.aws.amazon.com/cli/latest/userguide/installing.html
echo \# Then navigate to where you would like the files copied and run the commands below within the next 36 hours.
echo \# If you want to just check what the command will do, add the '--dryrun' option.
echo \# If the sync fails due to a broken connection, you can run it again and it will restart where it left off.
echo
echo \# Mac/Linux instructions:
echo  export AWS_ACCESS_KEY_ID=$ACCESSKEY
echo  export AWS_SECRET_ACCESS_KEY=$SECRETKEY
echo  export AWS_SESSION_TOKEN=$TOKEN
echo  aws s3 sync s3:/$s3_bucket/$s3_path .
echo
echo \# Windows instructions \(comment out the above section and run this instead\):
echo \# SET AWS_ACCESS_KEY_ID=$ACCESSKEY
echo \# SET AWS_SECRET_ACCESS_KEY=$SECRETKEY
echo \# SET AWS_SESSION_TOKEN=$TOKEN
echo \# aws s3 sync s3://$s3_bucket/$s3_path .
