#!/usr/bin/env python3
"""
Script to read all DynamoDB records, convert them to MeetupEvent items,
and reserialize them back to the DynamoDB table.

This script is useful for:
- Migrating data after schema changes
- Updating serialization format
- Fixing data inconsistencies
"""

import boto3
from boto3.dynamodb import conditions
from events import MeetupEvent, ai_categorize
from shared import shared
import sys

def scan_all_event_records():
    """
    Scan all records from DynamoDB table where id='event'
    Returns a list of raw DynamoDB items
    """
    print("Scanning all event records from DynamoDB...")
    
    # Use boto3 directly to get raw items for scanning
    dynamodb = boto3.resource('dynamodb', region_name='us-east-2')
    table = dynamodb.Table('RallyBot')
    
    # Scan for all items where id='event'
    response = table.scan(
        FilterExpression=conditions.Attr('id').eq('event')
    )
    
    items = response.get('Items', [])
    
    # Handle pagination if there are more items
    while 'LastEvaluatedKey' in response:
        print(f"Found {len(items)} items so far, continuing scan...")
        response = table.scan(
            FilterExpression=conditions.Attr('id').eq('event'),
            ExclusiveStartKey=response['LastEvaluatedKey']
        )
        items.extend(response.get('Items', []))
    
    print(f"Found {len(items)} total event records")
    return items


def convert_to_meetup_events(raw_items):
    """
    Convert raw DynamoDB items to MeetupEvent objects using the existing interface
    """
    print("Converting raw items to MeetupEvent objects...")
    meetup_events: list[MeetupEvent] = []
    
    for item in raw_items:
        try:
            # Use the existing unpickler to restore the object
            meetup_event = shared.ddb.unpickler.restore(item)
            
            # Ensure it's a MeetupEvent instance
            if isinstance(meetup_event, MeetupEvent):
                meetup_events.append(meetup_event)
                print(f"Successfully converted event: {meetup_event.sort} - {getattr(meetup_event, 'title', 'No title')}")
            else:
                print(f"Warning: Item with sort={item.get('sort')} is not a MeetupEvent instance")
                
        except Exception as e:
            print(f"Error converting item with sort={item.get('sort')}: {e}")
            continue
    
    print(f"Successfully converted {len(meetup_events)} MeetupEvent objects")
    return meetup_events


def reserialize_events(meetup_events: list[MeetupEvent], dry_run=True):
    """
    Reserialize MeetupEvent objects back to DynamoDB
    
    Args:
        meetup_events: List of MeetupEvent objects
        dry_run: If True, only print what would be done without actually writing
    """
    print(f"{'DRY RUN: ' if dry_run else ''}Reserializing {len(meetup_events)} events...")
    
    success_count = 0
    error_count = 0
    
    for event in meetup_events:
        try:
            event.timestamp = event.timestamp_start
            if dry_run:
                print(f"DRY RUN: Would reserialize event {event.sort} - {getattr(event, 'title', 'No title')}")
                print(f"Pickled: {shared.ddb.pickler.flatten(event)}")
            else:
                # lets re-do ai categorization while we're at it :3
                # print(f"Recategorizing {event.title} ...")
                # event.category = ai_categorize(f"{event.title}\n\n{event.description}")
                # Use the existing write_item method to reserialize
                shared.ddb.write_item(event)
                print(f"Successfully reserialized event {event.sort} - {getattr(event, 'title', 'No title')}")
            
            success_count += 1
            
        except Exception as e:
            print(f"Error reserializing event {event.sort}: {e}")
            error_count += 1
            continue
    
    print(f"{'DRY RUN: ' if dry_run else ''}Reserialization complete:")
    print(f"  Success: {success_count}")
    print(f"  Errors: {error_count}")
    
    return success_count, error_count


def main():
    """
    Main function to orchestrate the reserialization process
    """
    print("Starting DynamoDB MeetupEvent reserialization process...")
    
    # Check if this is a dry run
    dry_run = '--dry-run' in sys.argv or '-n' in sys.argv
    if dry_run:
        print("Running in DRY RUN mode - no actual writes will be performed")
    
    try:
        # Step 1: Scan all event records
        raw_items = scan_all_event_records()
        
        if not raw_items:
            print("No event records found in DynamoDB")
            return
        
        # Step 2: Convert to MeetupEvent objects
        meetup_events = convert_to_meetup_events(raw_items)
        
        if not meetup_events:
            print("No valid MeetupEvent objects found")
            return
        
        # Step 3: Reserialize back to DynamoDB
        success_count, error_count = reserialize_events(meetup_events, dry_run=dry_run)
        
        if not dry_run:
            print(f"\nReserialization completed successfully!")
            print(f"Total events processed: {len(meetup_events)}")
            print(f"Successfully reserialized: {success_count}")
            print(f"Errors encountered: {error_count}")
        else:
            print(f"\nDRY RUN completed!")
            print(f"Would process {len(meetup_events)} events")
            print(f"Run without --dry-run to perform actual reserialization")
        
    except Exception as e:
        print(f"Fatal error during reserialization process: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Print usage information
    if '--help' in sys.argv or '-h' in sys.argv:
        print("Usage: python reserialize_events.py [--dry-run|-n] [--help|-h]")
        print("")
        print("Options:")
        print("  --dry-run, -n    Run in dry-run mode (no actual writes)")
        print("  --help, -h       Show this help message")
        print("")
        print("This script reads all MeetupEvent records from DynamoDB,")
        print("converts them to MeetupEvent objects, and reserializes them")
        print("back to the table using the current serialization format.")
        sys.exit(0)
    
    main()
